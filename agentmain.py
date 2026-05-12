import os, sys, threading, queue, time, json, re, random, locale

_locale_name = (locale.getlocale()[0] or '').lower()
os.environ.setdefault(
    'GA_LANG',
    'zh' if any(k in _locale_name for k in ('zh', 'chinese')) else 'en',
)

if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
elif hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
elif hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='replace')

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from llmcore import (
    reload_mykeys,
    LLMSession,
    ToolClient,
    ClaudeSession,
    MixinSession,
    NativeToolClient,
    NativeClaudeSession,
    NativeOAISession,
    resolve_client,
)
from agent_loop import agent_runner_loop
from ga import GenericAgentHandler, smart_format, get_global_memory, format_error, consume_file

# 获取当前脚本所在文件夹路径
script_dir = os.path.dirname(os.path.abspath(__file__))


def load_tool_schema(suffix=''):
    global TOOLS_SCHEMA
    schema_path = os.path.join(script_dir, f'assets/tools_schema{suffix}.json')
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema_text = f.read()
    if os.name != 'nt':
        schema_text = schema_text.replace('powershell', 'bash')
    TOOLS_SCHEMA = json.loads(schema_text)


load_tool_schema()

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir):
    os.makedirs(mem_dir)

mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt):
    with open(mem_txt, 'w', encoding='utf-8') as f:
        f.write('# [Global Memory - L2]\n')

mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    template = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    text = ''
    if os.path.exists(template):
        with open(template, encoding='utf-8') as f:
            text = f.read()
    with open(mem_insight, 'w', encoding='utf-8') as f:
        f.write(text)

cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        tid = hex(random.randint(0, 99999999))[2:8]
        with open(cdp_cfg, 'w', encoding='utf-8') as f:
            f.write(f"const TID = '__ljq_{tid}';")
    except Exception as e:
        print(
            f'[WARN] CDP config init failed: {e} — '
            'advanced web features (tmwebdriver) will be unavailable.'
        )


def get_system_prompt():
    """读取项目里的系统提示词文件，再加上日期、全局记忆"""
    prompt_path = os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt')
    with open(prompt_path, 'r', encoding='utf-8') as f:
        prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    return prompt

class GenericAgent:
    """GenericAgent 负责加载 key、选择 LLM、维护任务队列、组装系统提示词并启动 loop"""

    def __init__(self):
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []
        self.handler = None
        self.task_queue = queue.Queue() # Agent 的“总任务队列”
        self.is_running = False
        self.stop_sig = False
        self.llm_no = 0
        self.inc_out = False
        self.verbose = True
        self.peer_hint = True
        self.log_path = os.path.join(script_dir, f'temp/model_responses/model_responses_{int(time.time()*1e6)%1000000:06d}.txt')
        self.load_llm_sessions()

    def load_llm_sessions(self):
        mykeys, changed = reload_mykeys()
        if not changed and hasattr(self, 'llmclients'):
            return
        try:
            oldhistory = self.llmclient.backend.history
        except:
            oldhistory = None

        llm_sessions = []
        for k, cfg in mykeys.items():
            if not any(x in k for x in ['api', 'config', 'cookie']):
                continue
            try:
                if 'mixin' in k:
                    llm_sessions.append({'mixin_cfg': cfg})
                else:
                    client = resolve_client(k)
                    if client:
                        llm_sessions.append(client)
            except:
                pass
        for i, s in enumerate(llm_sessions):
            if isinstance(s, dict) and 'mixin_cfg' in s:
                try:
                    mixin = MixinSession(llm_sessions, s['mixin_cfg'])
                    if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)):
                        llm_sessions[i] = NativeToolClient(mixin)
                    else:
                        llm_sessions[i] = ToolClient(mixin)
                except Exception as e:
                    print(
                        f'\n\n\n[ERROR] Failed to init MixinSession with cfg '
                        f'{s["mixin_cfg"]}: {e}!!!\n\n'
                    )

        self.llmclients = llm_sessions
        self.llmclient = self.llmclients[self.llm_no % len(self.llmclients)]
        if oldhistory:
            self.llmclient.backend.history = oldhistory

    def next_llm(self, n=-1):
        self.load_llm_sessions()
        self.llm_no = ((self.llm_no + 1) if n < 0 else n) % len(self.llmclients)
        lastc = self.llmclient
        self.llmclient = self.llmclients[self.llm_no]
        try:
            self.llmclient.backend.history = lastc.backend.history
        except:
            raise Exception('[ERROR] BAD Mixin config: Check your mykey.py')
        self.llmclient.last_tools = ''

        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name:
            load_tool_schema('_cn')
        else:
            load_tool_schema()

    def list_llms(self):
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]

    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict):
            return 'BADCONFIG_MIXIN'
        if model:
            return b.backend.model.lower()
        return f'{type(b.backend).__name__}/{b.backend.name}'

    def abort(self):
        if not self.is_running:
            return
        print('Abort current task...')
        self.stop_sig = True
        if self.handler is not None:
            self.handler.code_stop_signal.append(1)

    def put_task(self, query, source='user', images=None):
        display_queue = queue.Queue() # 某一次请求专属的结果队列
        self.task_queue.put(
            {
                'query': query,
                'source': source,
                'images': images or [],
                'output': display_queue,
            }
        )
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'):
            return raw_query

        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.join(script_dir, 'temp', v)
            if os.path.isfile(vfile):
                with open(vfile, encoding='utf-8') as f:
                    v = f.read().strip()
            try:
                v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError):
                pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put(
                {
                    'done': smart_format(f'✅ session.{k} = {repr(v)}', max_str_len=500),
                    'source': 'system',
                }
            )
            return None

        if raw_query.strip() == '/resume':
            return (
                '帮我看看最近有哪些会话可以恢复。读model_responses/目录，按修改时间取最近10个文件，'
                '从每个文件里找最后一个<history>...</history>块，用一句话总结每个会话在聊什么，'
                '列表给我选。注意读文件后要把字面的\\n替换成真换行才能正确匹配。'
            )
        return raw_query

    def run(self):
        while True:
            task = self.task_queue.get() # 阻塞等待
            raw_query = task['query']
            source = task['source']
            display_queue = task['output']

            # 先处理一些特殊命令
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done()
                continue

            self.is_running = True # 标记 Agent 正在运行
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f'[USER]: {rquery}') # 把用户问题加入历史记录

            # 生成系统提示词；如果当前 LLM backend 有额外系统提示词，就拼上，没有就用空字符串
            # getattr(对象, 属性名, 默认值)：安全获取对象 A 的属性 B，如果不存在，就返回默认值 C
            sys_prompt = get_system_prompt() + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            # 给 Agent 一个额外提示：如果用户提到其他会话或后台任务，
            # 可以去 temp/model_responses/ 找近期文件
            if self.peer_hint:
                sys_prompt += (
                    '\n[Peer] 用户提及其他会话/后台任务状态时: '
                    'temp/model_responses/ (只找近期修改的文件尾部)\n'
                )

            # hander: Agent 的“工具和上下文管理器”
            new_handler = GenericAgentHandler(self, self.history, os.path.join(script_dir, 'temp'))
            # 如果之前已经有旧 handler 并且旧 handler 的 working 里有 key_info
            if self.handler and 'key_info' in self.handler.working:
                # 那就把旧 key_info 搬到新 handler 里（搬是因为：每个新任务都会新建 handler）
                key_info = re.sub(
                    r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*',  # 匹配规则
                    '',                                     # 替换成空（即删除）
                    self.handler.working['key_info'],       # 源文本
                )
                new_handler.working['key_info'] = key_info
                
                # passed_sessions: 这个 key_info 已经跨过了几次新任务
                new_handler.working['passed_sessions'] = ps = (
                    self.handler.working.get('passed_sessions', 0) + 1
                )
                if ps > 0:
                    new_handler.working['key_info'] += (
                        f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，'
                        '若已在新任务，先更新或清除工作记忆。\n'
                    )
            self.handler = new_handler

            # although new handler, the **full** history is in llmclient, so it is full history!
            self.llmclient.log_path = self.log_path
            gen = agent_runner_loop(
                self.llmclient,
                sys_prompt,
                raw_query,
                new_handler,
                TOOLS_SCHEMA,
                max_turns=70,
                verbose=self.verbose,
            )

            try:
                full_resp = ''  # 保存当前任务到目前为止的完整输出
                last_pos = 0    # 记录上一次已经发给前端的位置
                for chunk in gen:  # agent_runner_loop() 运行
                    # 处理停止信号
                    if consume_file(self.task_dir, '_stop'):
                        self.abort()
                    if self.stop_sig:
                        break
                    full_resp += chunk
                    # 判断是否要把中间结果发给前端：新增内容超过50个字符 or 进入新一轮 LLM 运行
                    if len(full_resp) - last_pos > 50 or 'LLM Running' in chunk:
                        # 发送中间结果到结果队列
                        display_queue.put(
                            {
                                'next': full_resp[last_pos:] if self.inc_out else full_resp,
                                'source': source,
                            }
                        )
                        last_pos = len(full_resp)

                # 补发剩余增量: 如果最后还有一小段内容没达到 50 字，没有被发出去，这里补发
                if self.inc_out and last_pos < len(full_resp):
                    display_queue.put({'next': full_resp[last_pos:], 'source': source})
                # 整理 summary 格式
                if '</summary>' in full_resp:
                    full_resp = full_resp.replace('</summary>', '</summary>\n\n')
                #  整理 file_content 格式：在file_content外面套一层 ``` ```
                if '</file_content>' in full_resp:
                    full_resp = re.sub(
                        r'<file_content>\s*(.*?)\s*</file_content>',
                        r'\n````\n<file_content>\n\1\n</file_content>\n````',
                        full_resp,
                        flags=re.DOTALL,
                    )
                # 发送最终结果
                display_queue.put({'done': full_resp, 'source': source})
                self.history = new_handler.history_info
            except Exception as e:
                print(f'Backend Error: {format_error(e)}')
                display_queue.put(
                    {
                        'done': full_resp + f'\n```\n{format_error(e)}\n```',
                        'source': source,
                    }
                )
            finally:
                if self.stop_sig:
                    print('User aborted the task.')
                self.is_running = self.stop_sig = False
                self.task_queue.task_done()
                if self.handler is not None:
                    # 给 handler 的代码执行器发停止信号
                    self.handler.code_stop_signal.append(1)


GeneraticAgent = GenericAgent


if __name__ == '__main__':
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser()
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--llm_no', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--nobg', action='store_true')
    args, _unknown = parser.parse_known_args()
    _reflect_args = dict(zip([k.lstrip('-') for k in _unknown[::2]], _unknown[1::2])) if _unknown else {}

    if args.task and not args.nobg:
        import subprocess, platform

        cmd = [sys.executable, os.path.abspath(__file__)] + [
            a for a in sys.argv[1:] if a != '--nobg'
        ]
        cmd.append('--nobg')
        d = os.path.join(script_dir, f'temp/{args.task}')
        os.makedirs(d, exist_ok=True)
        p = subprocess.Popen(
            cmd,
            cwd=script_dir,
            creationflags=0x08000000 if platform.system() == 'Windows' else 0,
            stdout=open(os.path.join(d, 'stdout.log'), 'w', encoding='utf-8'),
            stderr=open(os.path.join(d, 'stderr.log'), 'w', encoding='utf-8'),
        )
        print(p.pid)
        sys.exit(0)

    agent = GeneraticAgent()
    agent.next_llm(args.llm_no)
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()

    if args.task:
        agent.peer_hint = False
        agent.task_dir = d = os.path.join(script_dir, f'temp/{args.task}')
        nround = ''
        infile = os.path.join(d, 'input.txt')

        if args.input:
            os.makedirs(d, exist_ok=True)
            import glob

            for f in glob.glob(os.path.join(d, 'output*.txt')):
                os.remove(f)
            with open(infile, 'w', encoding='utf-8') as f:
                f.write(args.input)

        if fh := consume_file(d, '_history.json'):
            agent.llmclient.backend.history = json.loads(fh)
        with open(infile, encoding='utf-8') as f:
            raw = f.read()

        while True:
            dq = agent.put_task(raw, source='task')
            while 'done' not in (item := dq.get(timeout=300)):
                if 'next' in item and random.random() < 0.95:  # 概率写一次中间结果
                    with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f:
                        f.write(item.get('next', ''))

            with open(f'{d}/output{nround}.txt', 'w', encoding='utf-8') as f:
                f.write(item['done'] + '\n\n[ROUND END]\n')
            consume_file(d, '_stop')  # 已经成功停下来了，避免打断下次reply

            for _ in range(300):  # 等reply.txt，10分钟超时
                time.sleep(2)
                if raw := consume_file(d, 'reply.txt'):
                    break
            else:
                break

            nround = nround + 1 if isinstance(nround, int) else 1
    elif args.reflect:
        agent.peer_hint = False
        import importlib.util

        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, 'init'):
            mod.init(_reflect_args)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}' + (f' args={_reflect_args}' if _reflect_args else ''))

        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try:
                    spec.loader.exec_module(mod)
                    _mt = os.path.getmtime(args.reflect)
                    if hasattr(mod, 'init'):
                        mod.init(_reflect_args)
                    print('[Reflect] reloaded')
                except Exception as e:
                    print(f'[Reflect] reload error: {e}')
            time.sleep(getattr(mod, 'INTERVAL', 5))
            try:
                task = mod.check()
            except Exception as e:
                print(f'[Reflect] check() error: {e}')
                continue
            if task and task == '/exit':
                break
            if task is None:
                continue

            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=180)):
                    pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False):
                    raise
                print(f'[Reflect] drain error: {e}')
                result = f'[ERROR] {e}'

            log_dir = os.path.join(script_dir, 'temp/reflect_logs')
            os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            log_path = os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')

            if on_done := getattr(mod, 'on_done', None):
                try:
                    on_done(result)
                except Exception as e:
                    print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False):
                print('[Reflect] ONCE=True, exiting.')
                break
    else:
        try:
            import readline
        except Exception:
            pass

        agent.inc_out = True
        while True:
            q = input('> ').strip()
            if not q:
                continue
            try:
                dq = agent.put_task(q, source='user')
                while True:
                    item = dq.get()
                    if 'next' in item:
                        print(item['next'], end='', flush=True)
                    if 'done' in item:
                        print()
                        break
            except KeyboardInterrupt:
                agent.abort()
                print('\n[Interrupted]')
