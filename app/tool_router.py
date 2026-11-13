"""Turn a small natural-language request into one validated local tool call."""

import json
import re
import urllib.error


def choice(values, description):
    return {'type': 'string', 'enum': values.split(), 'description': description}


def string(description):
    return {'type': 'string', 'description': description}


TOOLS = {
    'query_ledgers': ('查询账单与成本', '汇总原始客户账单应收金额、发件成本总金额、去重票数，或查指定运单。', {
        'scope': choice('bill cost both', 'bill=客户账单应收；cost=发件成本总金额；both=并列查询'),
        'group': choice('total day month party source', 'total=汇总，day=按日，month=按月，party=各客户或按客户，source=按来源文件'),
        'party': string('明确给出时逐字复制对象全名。否则省略。'),
        'waybill': string('明确给出时逐字复制运单号，保留前导零。否则省略。'),
        'metric': choice('amount tickets per_ticket', 'amount=金额，tickets=去重票数，per_ticket=单票金额'),
    }),
    'match_waybills': ('匹配运单', '对照两类数据，统计共有和单边运单数量。适用于按运单号匹配整批数据。', {}),
    'list_sources': ('查看数据来源', '列出已导入的来源文件、记录数和可用月份。', {}),
    'generate_daily': ('执行我的日报', '用户明确要生成或运行经营日报。按用户指定日期，否则按昨日。', {
        'date': string('可选。明确给出单日时填 YYYY-MM-DD。没有指定日期时省略。'),
    }),
    'search_documents': ('检索归档单据', '按客户、月份、类型或关键字检索本机文档，如查找罚款单、预览原始账单。', {
        'customer': string('用户明确给出的完整客户名称，逐字复制。'),
        'keyword': string('用户明确给出的文件名或关键词，逐字复制。'),
        'kind': choice('customer_bill cost hq_bill penalty claim complaint report other', '客户账单、发件成本、总部账单、罚款通知单、理赔结算单、客诉证据、经营报表、其他'),
        'status': choice('pending archived', '待确认或已归档'),
        'month': string('可选。用户明确指出年月时填 YYYY-MM。'),
    }),
    'explain_policy': ('查询口径说明', '查询日报、计算、归档、运行的规则或数据要求，不执行任务。', {
        'topic': choice('daily query archive offline', '日报、查询、归档、离线运行'),
    }),
    'plan_daily_pause': ('提出日报暂停申请', '用户请求临时暂停或恢复日报。此工具只生成待确认操作。', {
        'action': choice('pause resume', '暂停或恢复'),
    }),
    'unsupported': ('说明可执行范围', '用途不明确，或请求了本机不具备的功能，例如外部发送、随意执行指令。', {}),
}

REQUIRED = {'query_ledgers': ['scope', 'group'], 'explain_policy': ['topic'], 'plan_daily_pause': ['action']}
DECLARATIONS = [
    {'type': 'function', 'function': {'name': name, 'description': description,
     'parameters': {'type': 'object', 'properties': arguments, 'required': REQUIRED.get(name, []),
                    'additionalProperties': False}}}
    for name, (_, description, arguments) in TOOLS.items()
]
SYSTEM = ('你是离线快递财务系统的工具选择器。根据最后一条用户问题只选择一个工具。'
          '不得生成业务数据、报告或 SQL。用户说“生成日报”调用 generate_daily；'
          '问“日报怎么算”“为什么没生成”调用 explain_policy。说“按运单号匹配两类数据”调用 match_waybills；'
          '提供具体运单号查金额则调用 query_ledgers。问金额/票数走 query_ledgers；找原件走 search_documents。'
          '日期由本地程序校验。只使用定义的字段。查所有客户时省略 party；未给定具体运单号时省略 waybill。'
          '最后一条如在补充或更改此前问题的条件，沿用此前仍适用的分组和指标，以最新明确的账单或成本为准。'
          '“各客户”或“按客户”选 group=party，“单票”选 metric=per_ticket，“发件成本”选 scope=cost。'
          '只有要求按月汇总时才选 group=month，提到月份本身不会改变分组。'
          '查询账单与成本时，月份和环比由本地程序解析，不填任何额外字段。'
          '用户提出暂停或恢复日报，调用 plan_daily_pause。')
STRUCTURED_SCHEMA = {'anyOf': [
    {'type': 'object', 'properties': {
        'tool': choice(declaration['function']['name'], '所选工具'),
        'arguments': declaration['function']['parameters'],
    }, 'required': ['tool', 'arguments'], 'additionalProperties': False}
    for declaration in DECLARATIONS
]}
STRUCTURED_PROMPT = SYSTEM + '\n返回包含 tool 和 arguments 的 JSON。arguments 只填写所选工具的字段。\n' + '\n'.join(
    f'{name}：{description}。字段：{", ".join(fields) or "无"}'
    for name, (_, description, fields) in TOOLS.items()
)
RETRY_MESSAGE = '本地模型未能识别这次请求的条件。请用一句话说明要查哪类数据、哪个时间范围，或要执行什么任务。'


class RoutingError(ValueError):
    pass


class PlanError(RoutingError):
    pass


def validated(name, arguments, text, context):
    if not isinstance(name, str) or name not in TOOLS or not isinstance(arguments, dict):
        raise PlanError('模型未选出可执行的本地工具。')
    fields = TOOLS[name][2]
    if any(key not in fields for key in arguments):
        raise PlanError('工具参数超出范围。')
    values = {}
    request_text = '\n'.join([*context, text])
    for key, value in arguments.items():
        if value is None or value == '':
            continue
        allowed = fields[key].get('enum')
        if not isinstance(value, str) or not value.strip() or len(value) > 150:
            raise PlanError('工具参数格式无效。')
        if allowed and value not in allowed:
            raise PlanError('工具参数不在支持范围。')
        normalized = ((key == 'date' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)) or
                      (key == 'month' and re.fullmatch(r'\d{4}-\d{2}', value)))
        if allowed is None and not normalized and value not in request_text:
            raise RoutingError('请给出完整的客户名、运单号或文件名。')
        values[key] = value
    if any(key not in values for key in REQUIRED.get(name, ())):
        raise PlanError('模型未给出必要的工具参数。')
    return name, values


def validate_request(name, text):
    report = re.search(r'日报|每日经营|日经营报表', text)
    asks_policy = re.search(r'怎么|如何|口径|规则|为什么|为何|需要什么|说明', text)
    if report and not asks_policy and re.search(r'暂停|恢复|停用|停发|不(?:要|用|必)?(?:再)?(?:生成|跑|发)', text) and name != 'plan_daily_pause':
        raise RoutingError('模型未识别出日报暂停或恢复指令。')
    if name == 'generate_daily' and (not report or asks_policy):
        raise RoutingError('请明确输入“生成日报”及可选的结算日期。')
    if name == 'plan_daily_pause' and not (report and re.search(r'暂停|恢复|停用|停发|不(?:要|用|必)?(?:再)?(?:生成|跑|发)', text)):
        raise RoutingError('请明确指定日报的暂停或恢复操作。')


def select_tool(call, model, context_size, text, context=()):
    messages = [{'role': 'system', 'content': SYSTEM}]
    if context:
        messages.append({'role': 'user', 'content': '仅供理解这次追问的此前问题：\n' + '\n'.join(context)[-1200:]})
    messages.append({'role': 'user', 'content': text})
    base = {'model': model, 'messages': messages, 'stream': False,
            'options': {'temperature': 0, 'num_predict': 224, 'num_ctx': context_size}}
    try:
        reply = call('/api/chat', {**base, 'tools': DECLARATIONS}, 115)['message']
        calls = reply.get('tool_calls', [])
        if len(calls) == 1:
            function = calls[0]['function']
            arguments = function['arguments']
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            name, values = validated(function['name'], arguments, text, context)
            validate_request(name, text)
            return name, values, '原生工具调用'
    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise RoutingError('本地模型服务暂时无法完成工具选择。') from error
    except (RoutingError, KeyError, TypeError, AttributeError, ValueError):
        pass
    except OSError as error:
        raise RoutingError('本地模型暂未返回，请检查模型服务和内存状态。') from error

    try:
        plan = json.loads(call('/api/chat', {**base, 'messages': [{'role': 'system', 'content': STRUCTURED_PROMPT}, *messages[1:]],
                                            'format': STRUCTURED_SCHEMA}, 55)['message']['content'])
        if not isinstance(plan, dict) or plan.keys() != {'tool', 'arguments'}:
            raise PlanError('工具方案格式无效。')
        name, values = validated(plan['tool'], plan['arguments'], text, context)
        validate_request(name, text)
        return name, values, '结构化工具选择'
    except PlanError as error:
        raise RoutingError(RETRY_MESSAGE) from error
    except RoutingError:
        raise
    except OSError as error:
        raise RoutingError('本地模型暂未返回，请检查模型服务和内存状态。') from error
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise RoutingError(RETRY_MESSAGE) from error
