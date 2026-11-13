"""Start the packaged Windows services on separate loopback ports."""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import msvcrt
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
OLLAMA = ROOT / 'ollama' / 'ollama.exe'
SERVER = ROOT / 'app' / 'server.py'
MODELS = ROOT / 'models'
HTTP = build_opener(ProxyHandler({}))


class WindowClosed(Exception):
    pass


def watch_window(shutdown):
    try:
        # A daemon may still be waiting when startup fails. Read the OS pipe directly
        # so that it never holds Python's buffered stdin lock during interpreter shutdown.
        descriptor = sys.stdin.fileno()
        while os.read(descriptor, 1024):
            pass
    except (OSError, ValueError):
        pass
    finally:
        shutdown.set()


def check_window(shutdown):
    if shutdown.is_set():
        raise WindowClosed


def model_config():
    config_path = ROOT / '模型配置.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.is_file() else {}
    if not isinstance(config, dict):
        raise RuntimeError('模型配置必须是 JSON 对象。')
    name = config.get('model', 'qwen2.5:7b')
    context = config.get('context_tokens', 4096)
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,149}', name) or name.endswith(':cloud'):
        raise RuntimeError('模型配置请填写 Ollama 本机模型标签。')
    if type(context) is not int or not 2048 <= context <= 32768:
        raise RuntimeError('上下文长度须在 2048 至 32768 之间。')
    tag = name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'
    return name, tag, context


class BasicJobLimits(ctypes.Structure):
    _fields_ = [
        ('process_time_limit', ctypes.c_int64),
        ('job_time_limit', ctypes.c_int64),
        ('flags', wintypes.DWORD),
        ('minimum_working_set', ctypes.c_size_t),
        ('maximum_working_set', ctypes.c_size_t),
        ('active_process_limit', wintypes.DWORD),
        ('affinity', ctypes.c_size_t),
        ('priority_class', wintypes.DWORD),
        ('scheduling_class', wintypes.DWORD),
    ]


class ExtendedJobLimits(ctypes.Structure):
    _fields_ = [
        ('basic', BasicJobLimits),
        ('io_counters', ctypes.c_uint64 * 6),
        ('process_memory_limit', ctypes.c_size_t),
        ('job_memory_limit', ctypes.c_size_t),
        ('peak_process_memory', ctypes.c_size_t),
        ('peak_job_memory', ctypes.c_size_t),
    ]


def kernel_api(name, restype, argtypes):
    function = getattr(ctypes.WinDLL('kernel32', use_last_error=True), name)
    function.restype = restype
    function.argtypes = argtypes
    return function


@contextmanager
def child_job():
    """Close an unnamed Windows job to stop only this package's process tree."""
    handle_type = wintypes.HANDLE
    close = kernel_api('CloseHandle', wintypes.BOOL, [handle_type])
    create = kernel_api('CreateJobObjectW', handle_type, [ctypes.c_void_p, wintypes.LPCWSTR])
    set_limits = kernel_api('SetInformationJobObject', wintypes.BOOL,
                            [handle_type, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD])
    open_process = kernel_api('OpenProcess', handle_type, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    assign = kernel_api('AssignProcessToJobObject', wintypes.BOOL, [handle_type, handle_type])
    job = create(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    def start(command, **kwargs):
        process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        process_handle = open_process(0x0101, False, process.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        try:
            if not process_handle or not assign(job, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            process.kill()
            process.wait()
            raise
        finally:
            if process_handle:
                close(process_handle)
        return process

    try:
        limits = ExtendedJobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):  # JobObjectExtendedLimitInformation
            raise ctypes.WinError(ctypes.get_last_error())
        yield start
    finally:
        close(job)


@contextmanager
def package_lock():
    # Allow different copies to run on different ports, but one writer per data directory.
    with (DATA / '运行锁').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('这个文件夹的程序已经启动，请使用原窗口显示的浏览器地址。') from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def free_port():
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        return reservation.getsockname()[1]


def wait_for_json(process, endpoint, label, shutdown, seconds=75):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_window(shutdown)
        code = process.poll()
        if code is not None:
            raise RuntimeError(f'{label}已退出（代码 {code}）。')
        try:
            with HTTP.open(endpoint, timeout=1) as response:
                return json.load(response)
        except (OSError, ValueError):
            shutdown.wait(0.2)
    raise RuntimeError(f'{label}启动超时。')


def run(args):
    shutdown = threading.Event()
    if args.managed_window:
        threading.Thread(target=watch_window, args=(shutdown,), daemon=True).start()
    model, tag, context = model_config()
    required = (OLLAMA, SERVER, DATA / 'bill.sqlite3', DATA / 'cost.sqlite3', MODELS / 'manifests')
    for item in required:
        if not item.exists():
            raise RuntimeError(f'缺少 {item.relative_to(ROOT)}，请完整解压后再启动。')

    with package_lock(), tempfile.TemporaryFile(mode='w+b') as model_log, child_job() as start:
        check_window(shutdown)
        model_port = free_port()
        model_host = f'127.0.0.1:{model_port}'
        model_url = f'http://{model_host}'
        model_env = {key: value for key, value in os.environ.items() if not key.upper().startswith('OLLAMA_')}
        model_env.update(OLLAMA_HOST=model_host, OLLAMA_MODELS=str(MODELS), OLLAMA_NO_CLOUD='1')

        print('正在启动随包的本地模型…', flush=True)
        model_process = start([str(OLLAMA), 'serve'], cwd=OLLAMA.parent, env=model_env,
                              stdout=model_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            listing = wait_for_json(model_process, model_url + '/api/tags', 'Ollama', shutdown)
            if not any(item.get('name') == tag for item in listing.get('models', [])):
                raise RuntimeError(f'模型目录中没有找到 {tag}，请先放入匹配的本地模型。')
        except RuntimeError:
            model_log.seek(max(0, os.fstat(model_log.fileno()).st_size - 3500))
            print(model_log.read().decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
            raise

        check_window(shutdown)
        web_port = free_port()
        web_url = f'http://127.0.0.1:{web_port}/'
        web_env = dict(os.environ, FINANCE_OLLAMA_URL=model_url, FINANCE_MODEL=model,
                       FINANCE_MODEL_CONTEXT=str(context))
        web_process = start([sys.executable, '-X', 'utf8', '-B', str(SERVER), '--port', str(web_port),
                             '--data-dir', str(DATA)], cwd=SERVER.parent, env=web_env, stdin=subprocess.DEVNULL)
        status = wait_for_json(web_process, web_url + 'api/local/status', '网页服务', shutdown)
        if not status.get('ollama') or any(status.get('datasets', {}).get(kind, {}).get('state') != 'ready'
                                           for kind in ('bill', 'cost')):
            raise RuntimeError('网页未能连接随包模型，或两类数据未能载入。')

        check_window(shutdown)
        print(f'模型和两类数据已就绪。打开 {web_url}', flush=True)
        if args.check:
            return
        if args.managed_window:
            print(f'WEB_URL={web_url}', flush=True)
        else:
            if not args.no_browser:
                webbrowser.open(web_url)
            print('请保留此窗口。使用完毕后按 Ctrl+C 结束。', flush=True)
        while model_process.poll() is None and web_process.poll() is None:
            if shutdown.wait(0.5):
                return
        check_window(shutdown)
        raise RuntimeError('本地服务已停止，请重新启动。')


def main():
    parser = argparse.ArgumentParser(description='快递财务智能体 本地启动器')
    parser.add_argument('--check', action='store_true', help='检查模型和数据连接后退出')
    parser.add_argument('--no-browser', action='store_true', help='启动服务并显示地址')
    parser.add_argument('--managed-window', action='store_true', help=argparse.SUPPRESS)
    try:
        run(parser.parse_args())
    except WindowClosed:
        return 0
    except KeyboardInterrupt:
        print('\n已结束本地服务。', flush=True)
    except (OSError, RuntimeError, ValueError) as error:
        print(f'启动失败：{error}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
