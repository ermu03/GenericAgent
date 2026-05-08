import json, re, os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class StepOutcome:
    data: Any # 工具执行结果
    next_prompt: Optional[str] = None # 下一轮要给 LLM 的提示。如果为空，通常表示当前任务结束
    should_exit: bool = False # 是否强制退出整个 Agent 循环


def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, '__iter__') and not isinstance(ret, (str, bytes, dict, list)):
        ret = yield from ret
    return ret


class BaseHandler:
    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        return next_prompt

    def dispatch(self, tool_name, args, response, index=0):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index
            _ = yield from try_call_generator(
                self.tool_before_callback,
                tool_name,
                args,
                response,
            )
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _ = yield from try_call_generator(
                self.tool_after_callback,
                tool_name,
                args,
                response,
                ret,
            )
            return ret
        elif tool_name == 'bad_json':
            return StepOutcome(None, next_prompt=args.get('msg', 'bad_json'), should_exit=False)
        else:
            yield f"未知工具: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)


def json_default(o):
    return list(o) if isinstance(o, set) else str(o)


def exhaust(g):
    try:
        while True:
            next(g)
    except StopIteration as e:
        return e.value


def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy()
        data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace('\\n', '\n')


def agent_runner_loop(
    client,   # 当前 LLM 客户端
    system_prompt,
    user_input, # 用户输入，也就是本次任务内容
    handler,
    tools_schema,
    max_turns=40,
    verbose=True, # 是否输出详细过程
    initial_user_content=None, # 可选的初始用户内容；如果传了，就用它代替 user_input 发给模型
):
    """做 “LLM 响应 -> 解析工具调用 -> handler 执行 -> 工具结果回灌"""
    
    # 初始化 messages, 组装第一次发给 LLM 的消息
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": initial_user_content if initial_user_content is not None else user_input,
        },
    ]
    # 初始化运行状态
    turn = 0 # 当前第几轮
    handler.max_turns = max_turns
    exit_reason = {} # 记录为什么退出，比如任务完成、主动退出、达到最大轮数
    response = None # 保存当前轮 LLM 的回复

    # 主循环：最多跑 max_turns 轮
    while turn < handler.max_turns:
        turn += 1
        turnstr = f'LLM Running (Turn {turn}) ...'
        if handler.parent.task_dir: # 文件任务模式 python agentmain.py --task xxx
            turnstr = f'Turn {turn} ...'
        if verbose:
            turnstr = f'**{turnstr}**'
        yield f"\n\n{turnstr}\n\n"

        if turn % 10 == 0:
            client.last_tools = ''  # 每10轮重置一次工具描述，避免上下文过大导致的模型性能下降

        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            #  LLM 流式输出的内容原样继续 yield 出去。外层可以看到更详细的过程
            response = yield from response_gen
            yield '\n\n'
        else:
            # 不逐段展示详细输出，而是把生成器跑完，拿到完整 response，
            # 再清理一下内容，输出更简洁的文本。
            response = exhaust(response_gen)
            cleaned = _clean_content(response.content)
            if cleaned:
                yield cleaned + '\n'

        if not response.tool_calls:
            tool_calls = [{'tool_name': 'no_tool', 'args': {}}]
        else:
            tool_calls = [
                {
                    'tool_name': tc.function.name,
                    'args': json.loads(tc.function.arguments),
                    'id': tc.id,
                }
                for tc in response.tool_calls
            ]

        # 初始化本轮工具结果
        tool_results = []   # 保存工具执行结果，后面要回灌给 LLM
        next_prompts = set()  # 保存下一轮要发给 LLM 的提示
        exit_reason = {}   # 保存退出原因

        # 遍历执行每个工具调用
        for ii, tc in enumerate(tool_calls):
            tool_name = tc['tool_name']
            args = tc['args']
            tid = tc.get('id', '')

            if tool_name != 'no_tool':
                if verbose: # 参数完整展示
                    yield (
                        f"🛠️ Tool: `{tool_name}`  📥 args:\n"
                        f"````text\n{get_pretty_json(args)}\n````\n"
                    )
                else: # 只展示压缩版参数
                    yield f"🛠️ {tool_name}({_compact_tool_args(tool_name, args)})\n\n\n"

            # 交给 handler 执行工具
            handler.current_turn = turn
            gen = handler.dispatch(tool_name, args, response, index=ii)
            # 消费工具生成器
            try:
                first_value = next(gen)

                def proxy():
                    yield first_value
                    return (yield from gen)

                if verbose:
                    yield '`````\n'
                outcome = (yield from proxy()) if verbose else exhaust(proxy())
                if verbose:
                    yield '`````\n'
            except StopIteration as e:
                outcome = e.value

            # 判断是否退出
            if outcome.should_exit:
                exit_reason = {'result': 'EXITED', 'data': outcome.data}
                break
            # 如果没有下一轮 prompt，说明任务完成
            if not outcome.next_prompt:
                exit_reason = {'result': 'CURRENT_TASK_DONE', 'data': outcome.data}
                break
            # 处理未知工具
            if outcome.next_prompt.startswith('未知工具'):
                # 这里清空 `client.last_tools`，让下一轮重新给模型工具信息，减少模型继续调用错工具的概率
                client.last_tools = ''

            # 保存工具结果
            if outcome.data is not None and tool_name != 'no_tool':
                if type(outcome.data) in [dict, list]:
                    datastr = json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                else:
                    datastr = str(outcome.data)
                tool_results.append({'tool_use_id': tid, 'content': datastr})
            # 收集下一轮提示
            next_prompts.add(outcome.next_prompt)

        if len(next_prompts) == 0 or exit_reason:
            # 如果没有下一轮 prompt，或者已经有退出原因
            if len(handler._done_hooks) == 0 or exit_reason.get('result', '') == 'EXITED':
                # 就结束整个 while 循环
                break
            # 如果有 done hook，并且不是强制退出，就拿一个出来继续跑
            next_prompts.add(handler._done_hooks.pop(0))

        # 每轮结束回调
        next_prompt = handler.turn_end_callback(
            response,
            tool_calls,
            tool_results,
            turn,
            '\n'.join(next_prompts),
            exit_reason,
        )
        # 准备下一轮 messages
        messages = [
            {"role": "user", "content": next_prompt, "tool_results": tool_results}
        ]  # just new message, history is kept in *Session
    
    # 循环结束后的处理
    if exit_reason and response is not None:
        # 如果是因为 `exit_reason` 退出的，再调用一次 `turn_end_callback()` 做最终收尾
        handler.turn_end_callback(response, tool_calls, tool_results, turn, '', exit_reason)
    return exit_reason or {'result': 'MAX_TURNS_EXCEEDED'}


def _clean_content(text):
    if not text:
        return ''

    def _shrink_code(m):
        lines = m.group(0).split('\n')
        lang = lines[0].replace('```', '').strip()
        body = [l for l in lines[1:-1] if l.strip()]
        if len(body) <= 6:
            return m.group(0)
        preview = '\n'.join(body[:5])
        return f'```{lang}\n{preview}\n  ... ({len(body)} lines)\n```'

    text = re.sub(r'```[\s\S]*?```', _shrink_code, text)
    patterns = [
        r'<file_content>[\s\S]*?</file_content>',
        r'<tool_(?:use|call)>[\s\S]*?</tool_(?:use|call)>',
        r'(\r?\n){3,}',
    ]
    for p in patterns:
        text = re.sub(p, '\n\n' if '\\n' in p else '', text)
    return text.strip()


def _compact_tool_args(name, args):
    a = {k: v for k, v in args.items() if k != '_index'}
    for k in ('path',):
        if k in a:
            a[k] = os.path.basename(a[k])

    if name == 'update_working_checkpoint':
        s = a.get('key_info', '')
        return (s[:60] + '...') if len(s) > 60 else s

    if name == 'ask_user':
        q = str(a.get('question', ''))
        cs = a.get('candidates') or []
        if cs:
            q += '\ncandidates:\n' + '\n'.join(f'- {c}' for c in cs)
        return q

    s = json.dumps(a, ensure_ascii=False)
    return (s[:120] + '...') if len(s) > 120 else s
