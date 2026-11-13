/* Runtime bindings for the unchanged index-b.html sample. */
(() => {
  'use strict';

  const q = (selector) => document.querySelector(selector);
  const qa = (selector) => [...document.querySelectorAll(selector)];
  const pending = (text) => '<div class="bubble"><span class="typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span> ' + esc(text) + '</span></div>';
  const docs = () => window.DOCS || [];
  const taskRun = { id: '', exports: [] };
  const sourceLabels = new Map();
  let importedSources = [];
  let docsRequest;
  let importPoll;
  let saveQuestion = '';

  async function request(url, options) {
    const response = await apiFetch(url, options);
    const body = await response.json();
    if (!response.ok || body.error || body.ok === false) throw new Error(body.message || body.error || '本地服务请求失败');
    return body;
  }

  function post(url, value, signal) {
    return request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), signal });
  }

  function bubble(slot, text, failed = false) {
    const node = document.createElement('div');
    node.className = 'bubble';
    node.textContent = text;
    if (failed) Object.assign(node.style, { borderColor: '#F3CACA', background: '#FDECEC', color: '#8A2020' });
    slot.append(node);
  }

  function assistantSlot() {
    return appendAI('<div class="slot"></div>').querySelector('.slot');
  }

  function validId(id) {
    return typeof id !== 'undefined' && /^[A-Za-z0-9_-]+$/.test(String(id));
  }

  function archiveSource(name) {
    return docs().find((item) => name === item.name || name.startsWith(item.name + '/') || name.startsWith(item.name + ' ·'));
  }

  function sourceItems(sources) {
    const items = new Map();
    for (const value of sources || []) {
      const name = String(value);
      const doc = archiveSource(name);
      const label = doc?.name ? doc.name.replace(/["'\\]/g, '') : '本次用到的文档';
      const realId = doc && validId(doc.id);
      const id = realId ? String(doc.id) : 'source-only-' + sourceLabels.size;
      if (!realId) sourceLabels.set(id, name);
      if (!items.has(label)) items.set(label, { label, meta: '来自本地工具返回的来源记录', hit: [] });
      items.get(label).hit.push({ id, name });
    }
    return [...items.values()];
  }

  function sourceDetails(data, result = data.result) {
    const sources = result?.sources?.length ? result.sources : data.sources || [];
    const issues = [data.issues, data.missing, data.status?.missing].find((items) => Array.isArray(items) && items.length) || [];
    return { sources, issues };
  }

  function reportSources(data, result, mode, text) {
    const { sources, issues } = sourceDetails(data, result);
    if (!sources.length && !issues.length && mode !== 'task') return;
    sourceLabels.clear();
    const files = sourceItems(sources);
    EXEC.mode = mode;
    EXEC.lastText = text;
    EXEC.name = mode === 'task' ? (data.task?.name || EXEC.name || '我的任务') : '本次对话用到的数据';
    EXEC.need = files.concat(issues.map((item) => ({ label: '待补数据', meta: String(item), type: '_xlsx', hit: [] })));
    EXEC.srcMem = { ignored: [], added: [] };
    EXEC.basis = result?.notes || [];
    EXEC.state = 'chat';
    EXEC.step = 3;
    EXEC.focus = null;
    renderExec();
    epToggleSource(true);
  }

  const originalSourceItems = window.epSourceItems;
  window.epSourceItems = function () {
    return Array.isArray(EXEC.focus?.bridgeSources)
      ? sourceItems(EXEC.focus.bridgeSources).map((item) => ({ ...item, docs: item.hit })) : originalSourceItems();
  };

  function resultObject(result, data, query) {
    const columns = result.columns || [];
    const values = result.rows || [];
    const total = values.find((row) => row.includes('全部组织') || row.includes('合计'));
    const dailyMetrics = ['票数', '收入（不含税）', '毛利'];
    const kpis = total ? dailyMetrics.map((name) => ({ k: name, v: total[columns.indexOf(name)] })).filter((item) => item.v != null) : [];
    return {
      title: result.title,
      rows: [columns, ...values], total: values.length, kpis,
      basis: (result.notes || []).length ? result.notes : ['数据按当前本机工具返回的范围展示。'],
      basisVersion: result.basis_version || (data.tool?.name === 'generate_daily' ? data.status?.version : '') || '本地工具结果',
      source: (result.sources || []).join('、'), conclusion: result.conclusion || '',
      bridgeQuery: query, bridgeExports: data.exports || [],
      bridgeDaily: data.tool?.name === 'generate_daily' || data.task?.report_kind === 'daily',
      bridgeOutbound: data.report_kind === 'outbound_history',
    };
  }

  function safeLink(url) {
    return typeof url === 'string' && (url === '/api/local/reports/template' || /^\/api\/local\/(?:(?:reports|tasks|archive)\/file|reports\/outbound-file)\?/.test(url));
  }

  function download(exported) {
    if (!safeLink(exported?.url)) return false;
    const link = document.createElement('a');
    link.href = exported.url;
    link.download = exported.name || exported.label || '';
    document.body.append(link);
    link.click();
    link.remove();
    return true;
  }

  function responseActions(slot, data) {
    const actions = (data.actions || []).filter((action) => action.kind === 'confirm' || safeLink(action.url));
    if (data.status?.state === 'missing_source' && data.report_kind !== 'outbound_history' && !actions.some((action) => action.url === '/api/local/reports/template')) {
      actions.push({ label: '下载结算明细模板', name: '日报结算明细_导入模板.csv', url: '/api/local/reports/template' });
    }
    if (!actions.length) return;
    const row = document.createElement('div');
    row.className = 'draft-f';
    for (const action of actions) {
      const button = document.createElement('button');
      button.className = 'btn sm';
      button.textContent = action.label;
      button.onclick = async () => {
        if (safeLink(action.url)) { download(action); return; }
        if (!['pause', 'resume', 'enable'].includes(action.request?.action) || !window.confirm(action.label + '？')) return;
        button.disabled = true;
        try {
          await post('/api/local/tasks', { ...action.request, confirmed: true });
          toast('任务设置已更新');
          await loadTasks();
        } catch (error) {
          toast(error.message);
          button.disabled = false;
        }
      };
      row.append(button);
    }
    slot.append(row);
  }

  function renderResponse(slot, data, text, mode, active = true) {
    slot.replaceChildren();
    const result = data.result;
    if (data.answer || !result) bubble(slot, data.answer || data.message || '本次未返回可展示的结果。');
    if (result) {
      const card = resultCard(resultObject(result, data, text));
      card.querySelectorAll('tbody tr').forEach((row, index) => {
        row.onclick = () => {
          reportSources(data, result, mode, text);
          const label = [...row.cells].slice(0, 2).map((cell) => cell.textContent).join(' · ');
          epShowSource({ label, bridgeSources: result.row_sources?.[index] || result.sources || [] });
        };
      });
      card.querySelectorAll('.kpi').forEach((item) => {
        item.onclick = () => {
          reportSources(data, result, mode, text);
          epShowSource({ label: item.textContent, bridgeSources: result.sources || [] });
        };
      });
      slot.append(card);
    }
    responseActions(slot, data);
    if (active) {
      reportSources(data, result, mode, text);
      scrollMsgs();
    }
  }

  const sessionsUrl = '/api/local/chat/sessions';
  const originalSwitchSession = window.switchSession;
  const originalNewChat = q('#btnNew').onclick;
  let selecting = Promise.resolve();

  function sessionTime(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';
  }

  window.renderHistoryList = function () {
    q('#histList').innerHTML = SESSIONS.map((session) => {
      const last = String(session.history.at(-1)?.content || session.preview || '').replace(/\s+/g, ' ').slice(0, 16);
      return '<div class="hi' + (CUR === session ? ' on' : '') + '" onclick="switchSession(\'' + session.id + '\')">' +
        '<div class="t">' + esc(session.title) + '</div><div class="d">' + esc(session.time) +
        (last ? ' · ' + esc(last) : '· 空') + '</div></div>';
    }).join('');
  };

  function chooseSavedSession(session) {
    selecting = selecting.catch(() => {}).then(async () => {
      const { session: saved } = await post(sessionsUrl, { id: session.id });
      session.saved = true;
      session.time = sessionTime(saved.created_at) || session.time;
      renderHistoryList();
    });
    return selecting;
  }

  function getMessages(session, before) {
    const params = new URLSearchParams({ session_id: session.id });
    if (before != null) params.set('before', before);
    return request('/api/local/chat/messages?' + params);
  }

  function renderStoredAssistant(slot, message) {
    slot.replaceChildren();
    if (message.state === 'pending') {
      slot.innerHTML = pending(message.mode === 'task' ? '正在执行…' : '正在思考…');
    } else if (message.state !== 'error' && message.response) {
      renderResponse(slot, message.response, message.query || '', message.mode || 'chat', false);
    } else {
      bubble(slot, message.content || '本次未返回可展示的结果。', message.state === 'error');
    }
  }

  function messagePage(session, messages) {
    const page = document.createDocumentFragment();
    const entries = [];
    for (const message of messages) {
      const user = message.role === 'user';
      if (user && message.mode === 'task') continue;
      const entry = { role: message.role, content: message.content || '', response: message.response,
        query: message.query, mode: message.mode };
      entries.push(entry);
      const row = document.createElement('div');
      row.className = user ? 'msg u' : 'msg';
      row.innerHTML = user ? '<div class="who">李</div><div class="body"><div class="nm">李会计</div><div class="slot"></div></div>'
        : '<div class="who">AI</div><div class="body"><div class="nm">智能助手</div><div class="slot"></div></div>';
      const slot = row.querySelector('.slot');
      if (user) bubble(slot, entry.content);
      else {
        renderStoredAssistant(slot, message);
        if (message.state === 'pending') session.waiting.set(message.request_id, { slot, entry });
      }
      page.append(row);
    }
    return { page, entries };
  }

  function restoreChatContext(session) {
    const latestUser = session.history.findLast((message) => message.role === 'user');
    const response = session.history.findLast((message) => message.role === 'assistant')?.response;
    window.LAST_Q = latestUser?.content || '';
    taskRun.id = '';
    taskRun.exports = response?.exports || [];
    const sourceMessage = session.history.findLast((message) => {
      if (message.role !== 'assistant' || !message.response) return false;
      const { sources, issues } = sourceDetails(message.response);
      return sources.length || issues.length || message.mode === 'task';
    });
    if (sourceMessage) {
      const { response: data, query, mode } = sourceMessage;
      reportSources(data, data.result, mode || 'chat', query || '');
    } else {
      Object.assign(EXEC, { name: '', lastText: '', need: [], basis: [], focus: null, state: 'idle' });
      sourceLabels.clear();
      renderExec();
      epToggleSource(false);
    }
  }

  function watchPending(session) {
    clearTimeout(session.poll);
    if (CUR !== session || !session.waiting?.size) return;
    session.poll = setTimeout(async () => {
      if (CUR !== session) return;
      try {
        const data = await getMessages(session);
        if (CUR !== session) return;
        let completed = false;
        for (const message of data.messages) {
          if (message.role !== 'assistant' || message.state === 'pending') continue;
          const turn = session.waiting.get(message.request_id);
          if (!turn) continue;
          renderStoredAssistant(turn.slot, message);
          Object.assign(turn.entry, { content: message.content || '', response: message.response });
          session.waiting.delete(message.request_id);
          completed = true;
        }
        if (completed) {
          restoreChatContext(session);
          renderHistoryList();
        }
      } catch { /* Retry a temporary disconnect while a saved turn is still running. */ }
      watchPending(session);
    }, 1500);
  }

  function loadSession(session) {
    if (session.loaded) return Promise.resolve();
    if (session.loading) return session.loading;
    session.loading = (async () => {
      const data = await getMessages(session);
      session.waiting = new Map();
      const { page, entries } = messagePage(session, data.messages);
      if (entries.length) session.msgsEl.replaceChildren(page);
      session.history.push(...entries);
      session.nextBefore = data.next_before;
      session.loaded = true;
      if (CUR === session) {
        if (!entries.length && !session.msgsEl.childElementCount) addWelcomeAI();
        restoreChatContext(session);
        renderHistoryList();
        scrollMsgs();
        watchPending(session);
      }
    })().finally(() => { session.loading = null; });
    return session.loading;
  }

  function selectSession(session) {
    if (CUR !== session) originalSwitchSession(session.id);
    restoreChatContext(session);
    if (session.loaded && !session.msgsEl.childElementCount) addWelcomeAI();
    watchPending(session);
  }

  const historyReady = (async () => {
    const data = await request(sessionsUrl);
    if (!data.sessions.length) return;
    const available = data.sessions.filter((session) => validId(session.id));
    if (!available.length) return;
    SESSIONS.forEach((session) => session.msgsEl.remove());
    SESSIONS.length = 0;
    for (const saved of [...available].reverse()) {
      const session = newSession(saved.title);
      Object.assign(session, { id: saved.id, time: sessionTime(saved.created_at), preview: saved.preview, saved: true });
    }
    const session = SESSIONS.find((item) => item.id === data.active_id) || SESSIONS[0];
    selectSession(session);
    await loadSession(session);
  })().catch((error) => { toast('读取对话历史失败：' + error.message); });

  window.switchSession = async function (id) {
    await historyReady;
    const session = SESSIONS.find((item) => item.id === id);
    if (!session) return;
    selectSession(session);
    try {
      await Promise.all([chooseSavedSession(session), loadSession(session)]);
    } catch (error) { toast('切换对话失败：' + error.message); }
  };

  q('#btnNew').onclick = async function () {
    await historyReady;
    const session = { id: crypto.randomUUID() };
    try {
      await chooseSavedSession(session);
      // Keep the sample's own empty-session greeting and sidebar interaction.
      originalNewChat.call(this);
      const created = SESSIONS[0];
      Object.assign(created, { ...session, loaded: true });
      selectSession(created);
      renderHistoryList();
    } catch (error) { toast('新建对话失败：' + error.message); }
  };

  q('#msgs').addEventListener('scroll', async (event) => {
    const session = CUR;
    const area = event.currentTarget;
    if (area.scrollTop > 48 || session?.nextBefore == null || session.loadingOlder) return;
    session.loadingOlder = true;
    try {
      const data = await getMessages(session, session.nextBefore);
      if (CUR !== session) return;
      const before = area.scrollHeight;
      const top = area.scrollTop;
      const { page, entries } = messagePage(session, data.messages);
      session.msgsEl.prepend(page);
      session.history.unshift(...entries);
      session.nextBefore = data.next_before;
      area.scrollTop = top + area.scrollHeight - before;
      watchPending(session);
    } catch (error) { toast('读取更早对话失败：' + error.message); }
    finally { session.loadingOlder = false; }
  });

  async function prepareTurn(session) {
    if (!session.saved) {
      session.id = crypto.randomUUID();
      await chooseSavedSession(session);
      session.loaded = true;
    } else {
      await loadSession(session);
    }
    return { session_id: session.id, request_id: crypto.randomUUID() };
  }

  function csv(rows) {
    return rows.map((row) => row.map((value) => {
      let text = String(value ?? '');
      if (/^\s*[=+@-]/.test(text) && !/^-?\d+(?:\.\d+)?%?$/.test(text)) text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    }).join(',')).join('\r\n');
  }

  function exportCsv(rows, filename) {
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob(['\ufeff', csv(rows)], { type: 'text/csv;charset=utf-8' }));
    link.download = filename.replace(/[\\/:*?"<>|]/g, '_');
    document.body.append(link);
    link.click();
    setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
  }

  window.exportResult = function (result) {
    const report = result.bridgeExports?.find((item) => item.format === (result.bridgeOutbound ? 'xlsx' : 'csv'));
    if (download(report)) return;
    exportCsv([[result.title || '执行结果'], ['数据来源', result.source], ['口径版本', result.basisVersion],
      ['导出时间', new Date().toLocaleString('zh-CN')], ['计算口径', ...(result.basis || [])], [], ...(result.rows || []),
      ...(result.conclusion ? [['结论', result.conclusion]] : [])], `${result.title || '执行结果'}.csv`);
  };

  window.send = async function () {
    const input = q('#inp');
    const text = input.value.trim();
    if (!text || streaming) return;
    if (text.length > 500) { toast('请输入不超过 500 字的查询'); return; }
    setStreaming(true);
    ctrl = new AbortController();
    let slot;
    try {
      await historyReady;
      const session = CUR;
      const turn = await prepareTurn(session);
      if (CUR !== session || ctrl.signal.aborted) return;
      const context = session.history.filter((message) => message.role === 'user').slice(-8).map((message) => message.content);
      if (input.value.trim() === text) input.value = '';
      taskRun.id = '';
      taskRun.exports = [];
      window.LAST_Q = text;
      addUser(text);
      setCurTitle(text);
      session.history.push({ role: 'user', content: text });
      slot = assistantSlot();
      slot.innerHTML = pending('正在思考…');
      const data = await post('/api/local/query', { text, context, ...turn }, ctrl.signal);
      session.history.push({ role: 'assistant', content: data.answer || '', response: data, query: text, mode: 'chat' });
      renderResponse(slot, data, text, 'chat', CUR === session);
      if (CUR === session && data.tool?.name === 'generate_daily') {
        taskRun.exports = data.exports || [];
        const report = taskRun.exports.find((item) => item.format === 'csv');
        if (report) download(report);
        void loadTasks();
      }
      usage.turns += 1;
      usage.chars += (data.answer || '').length;
    } catch (error) {
      if (slot) {
        slot.replaceChildren();
        bubble(slot, error.name === 'AbortError' ? '已停止生成。' : error.message, error.name !== 'AbortError');
      } else if (error.name !== 'AbortError') {
        toast('无法保存对话：' + error.message);
      }
    } finally {
      ctrl = null;
      setStreaming(false);
      const usageState = q('#usageState');
      if (usageState) usageState.textContent = `${usage.turns} 轮 · ${usage.chars} 字`;
      renderHistoryList();
      scrollMsgs();
    }
  };
  q('#sendBtn').onclick = window.send;

  async function executeTask(task) {
    if (!task || streaming) return;
    setStreaming(true);
    ctrl = new AbortController();
    let slot;
    let session;
    try {
      await historyReady;
      session = CUR;
      const turn = await prepareTurn(session);
      if (CUR !== session || ctrl.signal.aborted) return;
      const text = [...(task.source || task.name || '运行任务')].slice(0, 500).join('');
      setCurTitle(text);
      taskRun.id = task.id;
      taskRun.exports = [];
      epGotoChat();
      EXEC.name = task.name;
      EXEC.need = [];
      EXEC.basis = [];
      EXEC.mode = 'task';
      EXEC.state = 'run';
      EXEC.step = 0;
      renderExec();
      epToggleSource(true);
      slot = assistantSlot();
      slot.innerHTML = pending('正在执行…');
      const data = await post('/api/local/tasks', { action: 'run', id: task.id, chat_text: text, ...turn }, ctrl.signal);
      session.history.push({ role: 'assistant', content: data.answer || '', response: data, query: text, mode: 'task' });
      renderResponse(slot, data, text, 'task', CUR === session);
      if (CUR === session) {
        taskRun.exports = data.exports || [];
        const report = taskRun.exports.find((item) => item.format === 'csv' || item.filename?.endsWith('.csv'));
        if (report) download(report);
      }
      void loadTasks();
    } catch (error) {
      if (slot) {
        slot.replaceChildren();
        bubble(slot, error.name === 'AbortError' ? '已停止等待。' : error.message, error.name !== 'AbortError');
        if (CUR === session) {
          EXEC.state = 'idle';
          renderExec();
        }
      } else if (error.name !== 'AbortError') {
        toast('无法保存对话：' + error.message);
      }
    } finally {
      ctrl = null;
      setStreaming(false);
      renderHistoryList();
    }
  }

  window.runTask = (index) => executeTask(TASKS[index]);
  window.dailyReportShortcut = () => quickSend('生成日报');
  window.downloadDailyReport = function () {
    const report = taskRun.exports.find((item) => item.format === 'csv');
    if (!download(report)) toast('还没有生成可下载的日报文件');
    return report?.name || '';
  };
  window.epFinish = function () {
    if (taskRun.exports.length) return window.downloadDailyReport();
    const task = TASKS.find((item) => item.id === taskRun.id);
    if (task) return executeTask(task);
    if (EXEC.lastText) return quickSend(EXEC.lastText);
    toast('请先运行任务或发送查询');
  };

  function refreshTaskStats() {
    const values = qa('#view-tasks .stat .s .v');
    values.forEach((item) => { item.textContent = '—'; });
    return request('/api/local/tasks').then((data) => {
      const tasks = data.tasks || [];
      if (values[0]) values[0].textContent = String(tasks.filter((task) => task.enabled && !task.paused_until).length);
      const runs = data.runs || [];
      const since = Date.now() - 7 * 864e5;
      const recent = runs.filter((run) => Date.parse(run.finished_at) >= since && ['succeeded', 'failed'].includes(run.status));
      if (values[1] && recent.length && (runs.length < 30 || Date.parse(runs.at(-1)?.started_at) < since)) {
        values[1].textContent = `${(100 * recent.filter((run) => run.status === 'succeeded').length / recent.length).toFixed(1)}%`;
      }
      if (values[2]) values[2].textContent = runs.length < 30 ? String(runs.length) : '≥30';
      return request('/api/local/audit?action=' + encodeURIComponent('任务告警') + '&limit=100');
    }).then((data) => {
      if (values[3]) values[3].textContent = data.entries?.length ? String(data.entries.length) : '0';
    }).catch(() => {});
  }

  window.loadTasks = async function () {
    try {
      const data = await request('/api/tasks');
      window.TASKS = data.tasks || [];
      window.TEMPS = TASKS.filter((task) => task.pausedUntil).map((task) => ({ task: task.name, action: 'pause', until: task.pausedUntil }));
      renderTasks();
      void refreshTaskStats();
    } catch (error) { toast('读取任务失败：' + error.message); }
  };

  const renderOriginalTasks = window.renderTasks;
  window.renderTasks = function () {
    window.TASKS = TASKS.filter((task) => task.id !== 'demo');
    renderOriginalTasks();
  };

  window.apiTasks = async function (body) {
    const payload = body.create ? { ...body, create: { ...body.create, query: saveQuestion } } : body;
    try {
      const data = await post('/api/tasks', payload);
      if (data.tasks) { window.TASKS = data.tasks; renderTasks(); }
      void refreshTaskStats();
      return data;
    } catch (error) {
      toast(error.message);
      return { ok: false, message: error.message };
    }
  };

  const openSave = window.openSaveTask;
  window.openSaveTask = function (result) {
    if (result?.bridgeOutbound) { toast('历史出港报表按指定日期生成'); return; }
    if (result?.bridgeDaily) { toast('日报已经由「我的日报」任务管理'); return; }
    if (!result?.bridgeQuery) { toast('请先通过对话生成一份查询结果'); return; }
    saveQuestion = result.bridgeQuery;
    const title = String(result.title || '我的任务');
    openSave({ ...result, title: title.replace(/["']/g, '') });
    q('#svName').value = title;
    q('#dwOk').onclick = async () => {
      const period = q('#svPeriod').value;
      const data = await apiTasks({ create: {
        name: q('#svName').value.trim() || '我的任务', period,
        to: q('#svTo').value.trim() || '—', channel: q('#svChannel').value,
        on: !period.includes('手动'), source: '对话保存',
      } });
      if (data.ok) { closeDrawer(); toast('已存为「' + (data.created?.name || title) + '」'); }
    };
  };

  const legacyOptions = ['每月 5 号 09:00', '每月 6 号 09:00', '工作日 08:30', '每月最后一个工作日 10:00', '—（仅手动执行）'];
  window.editTask = function (index) {
    const task = TASKS[index];
    if (!task) return;
    openDrawer('改一下：' + task.name,
      '<div class="field"><label>用一句话说你要改什么</label><textarea id="dwInput"></textarea></div>' +
      '<div class="field"><label>执行周期（只改参数，不改口径）</label><select id="dwPeriod"></select></div>' +
      '<div class="field"><label>推送给</label><input id="dwTo"></div>' +
      '<div class="tip">允许改：范围、时间、收件人、渠道。<b>不允许改口径</b>（公式、来源表、口径版本）——那属于新建任务。保存后版本号 +1，可回滚。</div>',
      async () => {
        const text = q('#dwInput').value.trim();
        let period = q('#dwPeriod').value;
        if (text) {
          const match = text.match(/每日\s*(\d{1,2}):(\d{2})|每[天日].*?(\d{1,2})点(\d{1,2})?/);
          if (!match) { toast('可填「每日 09:00」或直接选择执行周期'); return; }
          period = `每日 ${String(match[1] || match[3]).padStart(2, '0')}:${String(match[2] || match[4] || 0).padStart(2, '0')}`;
        }
        const data = await apiTasks({ id: task.id, patch: { period, to: q('#dwTo').value.trim(), next: period } });
        if (data.ok) { closeDrawer(); toast('已更新（版本 +1）'); }
      });
    for (const period of new Set([task.period, ...legacyOptions])) q('#dwPeriod').add(new Option(period));
    q('#dwPeriod').value = task.period;
    q('#dwTo').value = task.to || '';
  };

  window.clearTemp = async function (index) {
    const item = TEMPS[index];
    const task = TASKS.find((candidate) => candidate.name === item?.task);
    if (!task) return;
    try {
      await post('/api/local/tasks', { action: 'resume', id: task.id, confirmed: true });
      toast('已立即恢复');
      await loadTasks();
    } catch (error) { toast(error.message); }
  };

  async function upload(file) {
    const data = await request('/api/upload?name=' + encodeURIComponent(file.name), {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
    });
    if (data.state === 'queued') {
      void loadDocs();
      for (let attempt = 0; attempt < 150; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const status = (await request('/api/local/status')).datasets?.[data.kind];
        if (status?.error || status?.state === 'error') throw new Error(status.error || '索引导入失败');
        if (status?.state === 'ready') return { ...data, state: 'ready' };
      }
    }
    return data;
  }

  function importMessage(data) {
    if (data.kind === 'daily') return '日报结算数据已导入';
    if (data.state === 'queued') return '文件已接收，索引仍在导入';
    if (data.kind === 'bill' || data.kind === 'cost') return '索引已导入';
    return data.doc?.confident ? '已归档' : '已接收，待确认';
  }

  window.uploadFiles = async function (fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;
    const log = q('#uploadLog');
    log.replaceChildren();
    for (const [index, file] of files.entries()) {
      const row = document.createElement('div');
      row.style.fontSize = '12px';
      row.textContent = `${file.name}：正在上传和导入…`;
      log.append(row);
      try {
        const result = await upload(file);
        row.textContent = `${file.name}：${importMessage(result)}`;
        row.style.color = result.doc?.confident === false ? '#ED6C02' : '#2E7D32';
      } catch (error) {
        row.textContent = `${file.name}：${error.message}`;
        row.style.color = '#C62828';
      }
      q('#archBar').style.width = `${Math.round((index + 1) * 100 / files.length)}%`;
    }
    await loadDocs();
  };

  async function sourceUpload(input, index) {
    const file = input.files?.[0];
    if (!file) return;
    input.value = '';
    const note = typeof index === 'number' ? q('#epUp' + index) : null;
    if (note) note.textContent = '正在上传 ' + file.name + ' …';
    try {
      const data = await upload(file);
      await loadDocs();
      if (note) note.textContent = importMessage(data);
      toast(importMessage(data));
      if (data.kind === 'daily' && (EXEC.lastText || taskRun.id)) {
        EXEC.need = [];
        EXEC.state = 'wait';
        renderExec();
      }
    } catch (error) {
      if (note) note.textContent = error.message;
      toast(error.message);
    }
  }
  window.epUpload = (index, input) => sourceUpload(input, index);
  window.epAddDoc = (input) => sourceUpload(input);
  window.epPickNewDoc = function () {
    const input = q('#epNewFile');
    if (input) { input.accept = q('#fileInput').accept; input.click(); }
  };
  for (const method of ['epIgnoreDoc', 'epRestoreDoc', 'epRmFields', 'epIgnoreAdded']) {
    window[method] = () => toast('来源口径由后端工具控制，本次未修改');
  }

  function filteredDocs() {
    const customer = q('#fCust').value, month = q('#fMonth').value, kind = q('#fType').value;
    const keyword = q('#fKw').value.trim().toLowerCase();
    return docs().filter((doc) => (!customer || doc.customer === customer) && (!month || doc.month === month) &&
      (!kind || doc.type === kind) && (!keyword || `${doc.name}${doc.customer || ''}${doc.type || ''}`.toLowerCase().includes(keyword)));
  }

  function datasetSources(data) {
    const kinds = [
      ['bill', '客户账单索引', '按月汇总客户账单'],
      ['cost', '发件成本索引', '按月汇总发件成本'],
    ];
    const sources = kinds.flatMap(([kind, type, prompt]) => {
      const status = data.datasets?.[kind];
      if (!status || status.state === 'empty') return [];
      const months = status.months || [];
      return [{ ...status, name: status.name || type, type, prompt, months, period: months.join('、') }];
    });
    const report = data.report;
    if (report?.coverage) {
      const { first_day: first, last_day: last, rows } = report.coverage;
      const months = [first.slice(0, 7), last.slice(0, 7)];
      const period = first === last ? first : `${first} 至 ${last}`;
      sources.push({ name: report.source, type: '日报结算明细', state: 'ready', rows, months, period,
        prompt: `生成 ${last} 日报` });
    }
    return sources;
  }

  function importing(source) {
    return ['receiving', 'queued', 'importing'].includes(source.state);
  }

  function importStatus(source) {
    if (importing(source)) return source.state === 'receiving' ? '正在接收文件' : '正在建立索引';
    if (source.error) return `${source.state === 'ready' ? '替换失败，原索引可查询' : '导入失败'}（${source.error}）`;
    if (source.state === 'ready') return `已导入 ${Number(source.rows).toLocaleString('zh-CN')} 条`;
    return '导入失败';
  }

  function filteredSources() {
    const customer = q('#fCust').value, month = q('#fMonth').value, kind = q('#fType').value;
    const keyword = q('#fKw').value.trim().toLowerCase();
    return importedSources.filter((source) => !customer && (!kind || kind === source.type) &&
      (!month || source.months.includes(month) || (source.type === '日报结算明细' &&
        source.months[0] <= month && month <= source.months[1])) &&
      (!keyword || `${source.name}${source.type}`.toLowerCase().includes(keyword)));
  }

  function optionFill(select, values, all) {
    const value = select.value;
    select.replaceChildren(...(all ? [new Option(all, '')] : []), ...[...new Set(values.filter(Boolean))].map((item) => new Option(item, item)));
    if ([...select.options].some((option) => option.value === value)) select.value = value;
  }

  window.refreshFilterOptions = function () {
    optionFill(q('#fCust'), CUSTOMERS, '全部客户');
    optionFill(q('#fMonth'), [...recentMonths(12), ...docs().map((doc) => doc.month),
      ...importedSources.flatMap((source) => source.months)], '全部月份');
    optionFill(q('#fType'), [...sampleKinds, ...docs().map((doc) => doc.type),
      ...importedSources.map((source) => source.type)], '全部类型');
  };
  const sampleKinds = [...q('#fType').options].map((option) => option.value).filter(Boolean);
  // The original page retains its initial refresh callback for six seconds.
  // Use DOM option nodes even when that retained callback assigns innerHTML.
  const customerFilter = q('#fCust');
  const htmlProperty = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
  Object.defineProperty(customerFilter, 'innerHTML', {
    configurable: true,
    get() { return htmlProperty.get.call(this); },
    set() { optionFill(this, CUSTOMERS, '全部客户'); },
  });

  function button(text, listener) {
    const node = document.createElement('button');
    node.className = 'btn sm';
    node.textContent = text;
    node.onclick = listener;
    return node;
  }

  window.renderDocs = function () {
    const rows = filteredDocs();
    const sources = filteredSources();
    const body = q('#docRows');
    body.replaceChildren();
    for (const source of sources) {
      const row = body.insertRow();
      const filename = row.insertCell();
      filename.className = 'mono';
      filename.textContent = source.name;
      filename.title = `${source.name} · ${importStatus(source)}`;
      row.insertCell().textContent = '—';
      row.insertCell().textContent = source.period || '—';
      row.insertCell().textContent = source.type;
      const status = row.insertCell();
      status.className = 'r muted';
      status.textContent = importStatus(source);
      row.insertCell().textContent = '—';
      const actions = row.insertCell();
      actions.className = 'c';
      if (source.state === 'ready') actions.append(button('查询', () => {
        epGotoChat();
        q('#inp').value = source.prompt;
        q('#inp').focus();
      }));
    }
    for (const doc of rows) {
      const row = body.insertRow();
      const filename = row.insertCell();
      filename.className = 'mono';
      Object.assign(filename.style, { maxWidth: '260px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
      filename.textContent = doc.name;
      const customer = row.insertCell();
      if (doc.customer) customer.textContent = doc.customer;
      else customer.innerHTML = '<span class="tag warn">待指定</span>';
      row.insertCell().textContent = doc.month || '—';
      row.insertCell().textContent = doc.type || '—';
      const size = row.insertCell();
      size.className = 'r muted';
      size.textContent = fmtSize(doc.size || 0);
      row.insertCell().textContent = doc.uploadedBy || '';
      const actions = row.insertCell();
      actions.className = 'c';
      actions.append(button('预览', () => openFile(doc.id, 1)), ' ', button('下载', () => openFile(doc.id, 0)));
    }
    if (!rows.length && !sources.length) body.innerHTML = '<tr><td colspan="7" class="empty">没有符合条件的记录</td></tr>';
    q('#fCount').textContent = `单据 ${rows.length} 份 · 数据源 ${sources.length} 个`;
    const pendingCount = docs().filter((doc) => !doc.confident).length;
    const tip = q('#p-search .filters').nextElementSibling;
    const archiveTip = pendingCount ? `有 ${pendingCount} 份已上传单据待确认。` : '当前没有待确认单据。';
    tip.textContent = importedSources.length ? `账单、成本和日报明细可从「查询」进入对话。${archiveTip}` : archiveTip;
  };

  window.loadDocs = function () {
    if (docsRequest) return docsRequest;
    clearTimeout(importPoll);
    docsRequest = request('/api/docs').then((data) => {
      window.DOCS = data.docs || [];
      window.CUSTOMERS = data.customers || [];
      importedSources = datasetSources(data);
      renderArch();
      renderPending();
      if (importedSources.length) {
        q('#archLine').textContent = importedSources.map((source) => `${source.name}：${importStatus(source)}`).join(' · ');
        q('#docTotalBadge').textContent = docs().length + importedSources.length;
      }
      refreshFilterOptions();
      renderDocs();
      if (importedSources.some(importing)) importPoll = setTimeout(() => void loadDocs(), 3000);
    }).catch((error) => {
      q('#archStat').textContent = '读取失败：' + error.message;
    }).finally(() => { docsRequest = null; });
    return docsRequest;
  };
  q('.mi[data-view="docs"]').addEventListener('click', () => void loadDocs());
  for (const selector of ['#fCust', '#fMonth', '#fType']) q(selector).onchange = window.renderDocs;
  q('#fKw').oninput = window.renderDocs;

  window.fixDoc = function (id) {
    const doc = docs().find((item) => String(item.id) === String(id));
    if (!doc) return;
    openDrawer('指定归位：' + doc.name,
      '<div class="field"><label>客户</label><select id="fxCust"></select></div>' +
      '<div class="field"><label>月份</label><select id="fxMonth"></select></div>' +
      '<div class="field"><label>单据类型</label><select id="fxType"></select></div>' +
      '<div class="tip">指定后会按「客户-月份-类型」归位，并记入审计日志。</div>',
      async () => {
        try {
          await post('/api/docs', { id: doc.id, patch: { customer: q('#fxCust').value, month: q('#fxMonth').value, type: q('#fxType').value } });
          closeDrawer();
          toast('已归位');
          await loadDocs();
        } catch (error) { toast(error.message); }
      });
    const controls = [['#fxCust', [...CUSTOMERS, doc.customer], doc.customer],
      ['#fxMonth', [...recentMonths(12), doc.month], doc.month], ['#fxType', [...sampleKinds, doc.type], doc.type]];
    for (const [selector, values, chosen] of controls) {
      optionFill(q(selector), values, '请选择');
      q(selector).value = chosen || '';
    }
  };

  const openOriginal = window.openFile;
  window.openFile = function (id, inline) {
    const sourceName = sourceLabels.get(id);
    const doc = sourceName ? archiveSource(sourceName) : docs().find((item) => String(item.id) === String(id));
    if (sourceName && !validId(doc?.id)) { toast('此来源未登记可打开的原件'); return; }
    if (inline && doc && doc.previewable === false) { toast('该格式请通过下载查看'); return; }
    if (validId(doc?.id ?? id)) openOriginal(doc?.id ?? id, inline);
  };

  const auditBody = q('#p-audit tbody');
  auditBody.replaceChildren();
  async function loadAudit() {
    try {
      const data = await request('/api/audit');
      auditBody.replaceChildren();
      for (const item of data.entries || []) {
        const row = auditBody.insertRow();
        const time = row.insertCell();
        time.className = 'mono'; time.textContent = fmtTime(item.at);
        row.insertCell().textContent = item.actor || '—';
        const ip = row.insertCell();
        ip.className = 'mono'; ip.textContent = item.ip || '—';
        row.insertCell().textContent = item.action || '—';
        row.insertCell().textContent = item.target || '—';
        const result = row.insertCell();
        result.className = 'c';
        const label = document.createElement('span');
        label.className = 'tag ' + (/失败|拒绝/.test(item.result) ? 'err' : 'ok');
        label.textContent = item.result || '—';
        result.append(label);
      }
      if (!auditBody.rows.length) auditBody.innerHTML = '<tr><td colspan="6" class="empty">暂无审计日志</td></tr>';
    } catch (error) { toast('读取日志失败：' + error.message); }
  }
  const dd = window.ddGo;
  window.ddGo = function (pane) { dd(pane); if (pane === 'audit') void loadAudit(); };
  const auditTab = q('[data-pane="p-audit"]');
  const oldAuditTab = auditTab.onclick;
  auditTab.onclick = function (event) { oldAuditTab.call(this, event); void loadAudit(); };
  q('#p-audit .card-h .right button').onclick = () => { window.location.href = '/api/audit/export'; };
  q('#p-search .card-h .right button').onclick = () => exportCsv([['文件', '客户', '月份', '类型', '上传人'],
    ...filteredSources().map((source) => [source.name, '', source.period, source.type, '']),
    ...filteredDocs().map((doc) => [doc.name, doc.customer, doc.month, doc.type, doc.uploadedBy])], '单据清单.csv');

  function removeDemoState() {
    for (const node of qa('#msgs .bubble')) {
      if (node.textContent.includes('当前为演示模式（回复为预设内容）')) node.closest('.msg')?.remove();
    }
    const banner = q('#keyBanner span');
    if (banner) banner.textContent = '本地模型服务未就绪，请重新检测模型服务状态。';
    if (q('#connPill').textContent.includes('演示模式')) q('#connPill').textContent = '● 本地模型未就绪';
  }
  let lastConfig;
  function showConfig(config) {
    const status = q('#connPill');
    const text = config.hasKey ? '● 本地服务正常' : config.disconnected ? '● 服务未启动' : '● 本地模型未就绪';
    if (status.textContent !== text) status.textContent = text;
    status.className = 'pill ' + (config.hasKey ? 'ok' : 'bad');
    q('#keyBanner').style.display = config.hasKey || config.disconnected ? 'none' : 'flex';
    if (config.model) q('#navFoot').textContent = '数据不出内网 · ' + config.model;
    removeDemoState();
  }
  window.refreshConfig = async function () {
    try { lastConfig = await request('/api/config'); }
    catch { lastConfig = { hasKey: false, disconnected: true }; }
    window.cfg = lastConfig;
    showConfig(lastConfig);
    return lastConfig;
  };
  new MutationObserver(() => {
    if (lastConfig) showConfig(lastConfig);
  }).observe(q('#connPill'), { childList: true });

  q('#p-search .filters').nextElementSibling.textContent = '缺件提示：正在核对档案库。';
  void loadDocs();
  void refreshTaskStats();
  new MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes) {
      if (node.nodeType === 1 && node.classList.contains('msg') && node.textContent.includes('当前为演示模式（回复为预设内容）')) node.remove();
    }
  }).observe(q('#msgsAll'), { childList: true, subtree: true });
  setTimeout(removeDemoState, 0);
  void refreshConfig();
})();
