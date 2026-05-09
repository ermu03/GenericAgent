import base64, hashlib, json, os, queue, re, socket
import struct, sys, threading, time, uuid, webbrowser
from pathlib import Path
from urllib.parse import quote

import qrcode
import requests
from Crypto.Cipher import AES

# 把项目根目录加入 Python 搜索路径 -> from agentmain import GeneraticAgent
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 指向 GenericAgent/temp，后面微信收到的文件、Agent 生成的文件都主要放这里
_TEMP_DIR = os.path.join(PROJECT_ROOT, 'temp')

from agentmain import GeneraticAgent
from plugins.xhs_comment_analysis import fetch_xhs_note
from plugins.xhs_comment_analysis.url_utils import XhsUrlError

# ── WxBotClient (inline from wx_bot_client.py) ──
for _k in ('HTTPS_PROXY', 'https_proxy'):
    os.environ.pop(_k, None)  # avoid inherited proxy breaking WeChat long-poll SSL
API = 'https://ilinkai.weixin.qq.com'  # 微信机器人接口的基础地址
TOKEN_FILE = Path.home() / '.wxbot' / 'token.json'
TOKEN_FILE.parent.mkdir(exist_ok=True)
VER, MSG_USER, MSG_BOT, ITEM_TEXT, STATE_FINISH = '2.1.10', 1, 2, 1, 2
ILINK_APP_ID = 'bot'  # 微信 iLink API 请求头里的应用 ID：'iLink-App-Id': ILINK_APP_ID
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 10  # 把版本号 2.1.10 编码成一个整数：131338
UA = f'openclaw-weixin/{VER}'
ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO = 2, 4, 5
CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c'
WECHAT_TEXT_CHUNK_SIZE = 5000
WECHAT_TEXT_FILE_EXTS = {
    '.cfg', '.conf', '.css', '.csv', '.env', '.html', '.ini', '.js', '.json', '.jsx',
    '.log', '.md', '.py', '.sh', '.sql', '.toml', '.ts', '.tsx', '.txt', '.xml', '.yaml', '.yml',
}

def _uin():
    """生成一个“随机整数”的 Base64 字符串表示。"""
    raw = str(struct.unpack('>I', os.urandom(4))[0]).encode()
    return base64.b64encode(raw).decode()


def _split_text(text, limit=WECHAT_TEXT_CHUNK_SIZE):
    """按固定长度分片发送，避免单条过长，但不丢弃任何内容。"""
    text = (text or '').strip()
    if not text:
        return []
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _wechat_file_display_name(file_path):
    """微信打不开部分源码/配置扩展名；发送时仅改展示文件名，不改本地文件。"""
    fp = Path(file_path)
    if fp.suffix.lower() in WECHAT_TEXT_FILE_EXTS and fp.suffix.lower() != '.txt':
        return f'{fp.name}.txt'
    return fp.name


class WxBotClient:
    """
    微信 API 封装。

    主要能力：
    - _load() / _save(): 从 ~/.wxbot/token.json 读写登录 token。
    - _post(): 统一向 https://ilinkai.weixin.qq.com 发请求。
    - login_qr(): 没有 token 时生成二维码，让用户扫码登录。
    - get_updates(): 长轮询拉取微信新消息。
    - send_text(): 发文本消息。
    - send_typing(): 告诉微信“机器人正在输入”。
    - send_file() / send_image() / send_video(): 发送文件、图片、视频。
    - run_loop(on_message): 持续监听微信消息，收到后调用外部 on_message。
    """

    def __init__(self, token=None, token_file=None):
        self._tf = Path(token_file) if token_file else TOKEN_FILE
        self.token = token
        self.bot_id = None
        self._buf = ''
        if not self.token:
            self._load()

    def _load(self):
        if self._tf.exists():
            d = json.loads(self._tf.read_text('utf-8'))
            self.token = d.get('bot_token', '')
            self.bot_id = d.get('ilink_bot_id', '')
            self._buf = d.get('updates_buf', '')

    def _save(self, **kw):
        """将 token、id、buf、login_time 保存到 .wxbot/token.json"""
        d = {
            'bot_token': self.token or '',
            'ilink_bot_id': self.bot_id or '',
            'updates_buf': self._buf or '',
            **kw,
        }
        self._tf.write_text(json.dumps(d, ensure_ascii=False, indent=2), 'utf-8')

    def _post(self, ep, body, timeout=15):
        data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        h = {
            'Content-Type': 'application/json',
            'AuthorizationType': 'ilink_bot_token',
            'Content-Length': str(len(data)),
            'X-WECHAT-UIN': _uin(),
            'iLink-App-Id': ILINK_APP_ID,
            'iLink-App-ClientVersion': str(ILINK_APP_CLIENT_VERSION),
            'User-Agent': UA,
        }
        tok = (self.token or '').strip()
        if tok:
            h['Authorization'] = f'Bearer {tok}'
        r = requests.post(f'{API}/{ep}', data=data, headers=h, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def login_qr(self, poll_interval=2):
        """没有 token 时生成二维码，让用户扫码登录"""
        r = requests.get(
            f'{API}/ilink/bot/get_bot_qrcode',
            params={'bot_type': 3},
            headers={'User-Agent': UA},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        qr_id, url = d['qrcode'], d.get('qrcode_img_content', '')
        print(f'[QR登录] ID: {qr_id}')
        if url:
            img = self._tf.parent / 'wx_qr.png'
            qrcode.make(url).save(str(img))
            webbrowser.open(str(img))
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        last = ''
        while True:
            time.sleep(poll_interval) # 轮询 2 秒
            try:
                s = requests.get(
                    f'{API}/ilink/bot/get_qrcode_status',
                    params={'qrcode': qr_id},
                    headers={'User-Agent': UA},
                    timeout=60,
                ).json()
            except requests.exceptions.ReadTimeout:
                continue
            st = s.get('status', '')
            # 打印状态（重复不打印）
            if st != last:
                print(f'  状态: {st}')
                last = st
            if st == 'confirmed':
                self.token = s.get('bot_token', '')
                self.bot_id = s.get('ilink_bot_id', '')
                self._save(login_time=time.strftime('%Y-%m-%d %H:%M:%S'))
                print(f'[QR登录] 成功! bot_id={self.bot_id}')
                return s
            if st == 'expired':
                raise RuntimeError('二维码过期')

    def get_updates(self, timeout=30):
        """长轮询拉取微信新消息"""
        try:
            resp = self._post('ilink/bot/getupdates',
                              {'get_updates_buf': self._buf or '',
                               'base_info': {'channel_version': VER}},
                              timeout=timeout + 5)
        except requests.exceptions.ReadTimeout:
            return []
        if resp.get('errcode'):
            print(f'[getUpdates] err: {resp.get("errcode")} {resp.get("errmsg","")}')
            if resp['errcode'] == -14:
                self._buf = ''
                self._save()
            return []
        nb = resp.get('get_updates_buf', '')
        if nb:
            self._buf = nb
            self._save()
        return resp.get('msgs') or []

    def send_text(self, to_user_id, text, context_token=''):
        """发文本消息"""
        msg = {
            'from_user_id': '',
            'to_user_id': to_user_id,
            'client_id': f'pyclient-{uuid.uuid4().hex[:16]}',
            'message_type': MSG_BOT,
            'message_state': STATE_FINISH,
            'item_list': [{'type': ITEM_TEXT, 'text_item': {'text': text}}],
        }
        if context_token:
            msg['context_token'] = context_token
        return self._post(
            'ilink/bot/sendmessage',
            {'msg': msg, 'base_info': {'channel_version': VER}},
        )

    def send_typing(self, to_user_id, typing_ticket='', cancel=False):
        """告诉微信“机器人正在输入”或取消输入状态。"""
        return self._post(
            'ilink/bot/sendtyping',
            {
                'ilink_user_id': to_user_id,
                'typing_ticket': typing_ticket,
                'status': 2 if cancel else 1,
                'base_info': {'channel_version': VER},
            },
        )

    def get_typing_ticket(self, to_user_id, context_token=''):
        """从微信会话配置中获取 sendtyping 所需的 typing_ticket。"""
        payload = {'ilink_user_id': to_user_id}
        if context_token:
            payload['context_token'] = context_token
        return self._post('ilink/bot/getconfig', payload).get('typing_ticket', '')

    def _enc(self, raw, aes_key):
        """
        AES-ECB 模式的加密函数，作用是：对原始二进制数据进行 AES 对称加密
        """
        pad = 16 - (len(raw) % 16)
        return AES.new(aes_key, AES.MODE_ECB).encrypt(raw + bytes([pad] * pad))

    def _upload(self, filekey, upload_param, raw, aes_key, timeout=120, upload_url=''):
        if upload_url:
            url = upload_url.strip()
        else:
            url = f'{CDN_BASE}/upload?encrypted_query_param={quote(upload_param)}&filekey={filekey}'
        data = self._enc(raw, aes_key)
        last_err = None
        for attempt in range(1, 4):
            try:
                r = requests.post(
                    url,
                    data=data,
                    headers={'Content-Type': 'application/octet-stream', 'User-Agent': UA},
                    timeout=timeout,
                )
                if 400 <= r.status_code < 500:
                    msg = r.headers.get('x-error-message') or r.text[:300]
                    raise RuntimeError(f'CDN upload client error {r.status_code}: {msg}')
                if r.status_code != 200:
                    msg = r.headers.get('x-error-message') or f'status {r.status_code}'
                    raise RuntimeError(f'CDN upload server error: {msg}')
                eq = r.headers.get('x-encrypted-param', '')
                if not eq:
                    raise RuntimeError('CDN upload response missing x-encrypted-param header')
                return {
                    'encrypt_query_param': eq,
                    'aes_key': base64.b64encode(aes_key.hex().encode()).decode(),
                    'encrypt_type': 1,
                }
            except Exception as e:
                last_err = e
                if 'client error' in str(e) or attempt >= 3:
                    break
                print(f'[WX] CDN upload retry {attempt}: {e}', file=sys.__stdout__)
        raise last_err

    def _send_media(
        self,
        to_user_id,
        file_path,
        media_type,
        item_type,
        item_key,
        context_token='',
        display_name=None,
    ):
        fp = Path(file_path)
        raw = fp.read_bytes()
        filekey = uuid.uuid4().hex
        aes_key = os.urandom(16)
        ciphertext_size = ((len(raw) // 16) + 1) * 16
        thumb_raw = b''
        thumb_w = thumb_h = 0
        thumb_ciphertext_size = 0
        if item_key == 'image_item':
            from io import BytesIO
            from PIL import Image

            im = Image.open(fp)
            im.thumbnail((240, 240))
            thumb_w, thumb_h = im.size
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            bio = BytesIO()
            im.save(bio, format='JPEG', quality=85)
            thumb_raw = bio.getvalue()
            thumb_ciphertext_size = ((len(thumb_raw) // 16) + 1) * 16
        body = {
            'filekey': filekey,
            'media_type': media_type,
            'to_user_id': to_user_id,
            'rawsize': len(raw),
            'rawfilemd5': hashlib.md5(raw).hexdigest(),
            'filesize': ciphertext_size,
            'no_need_thumb': item_key not in ('image_item', 'video_item'),
            'aeskey': aes_key.hex(),
            'base_info': {'channel_version': VER},
        }
        if thumb_raw:
            body.update(
                {
                    'thumb_rawsize': len(thumb_raw),
                    'thumb_rawfilemd5': hashlib.md5(thumb_raw).hexdigest(),
                    'thumb_filesize': thumb_ciphertext_size,
                }
            )
        resp = self._post('ilink/bot/getuploadurl', body)
        upload_param = resp.get('upload_param', '')
        upload_url = resp.get('upload_full_url', '')
        if not (upload_param or upload_url):
            raise RuntimeError(f'getuploadurl failed: {resp}')
        media = self._upload(filekey, upload_param, raw, aes_key=aes_key, upload_url=upload_url)
        item = {'media': media}
        if item_key == 'file_item':
            item.update({'file_name': display_name or fp.name, 'len': str(len(raw))})
        elif item_key == 'image_item':
            thumb_param = resp.get('thumb_upload_param', '')
            thumb_url = resp.get('thumb_upload_full_url', '')
            if thumb_param or thumb_url:
                thumb_media = self._upload(
                    filekey,
                    thumb_param,
                    thumb_raw,
                    aes_key=aes_key,
                    upload_url=thumb_url,
                )
                thumb_size = thumb_ciphertext_size
            else:
                # Some getuploadurl responses only return a single upload_full_url for IMAGE.
                # Keep ImageItem structurally complete by reusing the original CDN media as thumb_media.
                thumb_media = media
                thumb_size = ciphertext_size
            item.update(
                {
                    'mid_size': ciphertext_size,
                    'thumb_media': thumb_media,
                    'thumb_size': thumb_size,
                    'thumb_width': thumb_w,
                    'thumb_height': thumb_h,
                }
            )
        elif item_key == 'video_item':
            item.update({'video_size': ciphertext_size})
        msg = {
            'from_user_id': '',
            'to_user_id': to_user_id,
            'client_id': f'pyclient-{uuid.uuid4().hex[:16]}',
            'message_type': MSG_BOT,
            'message_state': STATE_FINISH,
            'item_list': [{'type': item_type, item_key: item}],
        }
        if context_token:
            msg['context_token'] = context_token
        return self._post(
            'ilink/bot/sendmessage',
            {'msg': msg, 'base_info': {'channel_version': VER}},
        )

    def send_file(self, to_user_id, file_path, context_token='', display_name=None):
        return self._send_media(
            to_user_id,
            file_path,
            3,
            ITEM_FILE,
            'file_item',
            context_token,
            display_name=display_name,
        )

    def send_image(self, to_user_id, file_path, context_token=''):
        return self._send_media(to_user_id, file_path, 1, ITEM_IMAGE, 'image_item', context_token)

    def send_video(self, to_user_id, file_path, context_token=''):
        return self._send_media(to_user_id, file_path, 2, ITEM_VIDEO, 'video_item', context_token)

    @staticmethod
    def extract_text(msg):
        return '\n'.join(it['text_item'].get('text', '')
                         for it in msg.get('item_list', [])
                         if it.get('type') == ITEM_TEXT and it.get('text_item'))

    @staticmethod
    def is_user_msg(msg):
        return msg.get('message_type') == MSG_USER

    def run_loop(self, on_message, poll_timeout=30):
        """持续监听微信消息，收到后调用外部传入的 on_message(self, msg) 处理"""
        print(f'[Bot] 监听中... (bot_id={self.bot_id})')
        seen = set() # 创建一个集合，用来记录已经处理过的消息 ID
        while True:
            try:
                for msg in self.get_updates(poll_timeout):
                    mid = msg.get('message_id', 0)
                    if not self.is_user_msg(msg) or mid in seen:
                        continue
                    seen.add(mid)
                    if len(seen) > 1000:
                        seen = set(list(seen)[-500:])
                    try:
                        on_message(self, msg)
                    except Exception as e:
                        print(f'[Bot] 回调异常: {e}')
            except KeyboardInterrupt:
                print('[Bot] 退出')
                break
            except Exception as e:
                print(f'[Bot] 异常: {e}，5s重试')
                time.sleep(5)

# ── Unified media download (IMAGE/VIDEO/FILE/VOICE) ──
_MEDIA_KEYS = {'image_item': '.jpg', 'video_item': '.mp4', 'file_item': '', 'voice_item': '.silk'}

def _dl_media(items):
    """
    Download & decrypt all media items → list of local file paths.
    把消息里的媒体资源（图片/语音/视频等）从 CDN 拉下来并解密成可用文件
    """
    paths = []
    for item in items:
        for key, ext in _MEDIA_KEYS.items():
            sub = item.get(key)
            if not sub:
                continue
            eq = (sub.get('media') or {}).get('encrypt_query_param')
            if not eq:
                continue
            ak = (sub.get('media') or {}).get('aes_key', '') or sub.get('aeskey', '')
            if not ak:
                continue
            try:
                if sub.get('media', {}).get('aes_key'):
                    aes_key = bytes.fromhex(base64.b64decode(ak).decode())
                else:
                    aes_key = bytes.fromhex(ak)
                ct = requests.get(
                    f'{CDN_BASE}/download?encrypted_query_param={quote(eq)}',
                    headers={'User-Agent': UA},
                    timeout=60,
                ).content
                pt = AES.new(aes_key, AES.MODE_ECB).decrypt(ct)
                pt = pt[:-pt[-1]]
                fname = sub.get('file_name') or f'{uuid.uuid4().hex[:8]}{ext or ".bin"}'
                p = os.path.join(_TEMP_DIR, fname)
                with open(p, 'wb') as f:
                    f.write(pt)
                paths.append(p)
                print(f'[WX] media saved: {fname}', file=sys.__stdout__)
            except Exception as e:
                print(f'[WX] media dl err ({key}): {e}', file=sys.__stdout__)
            break  # one media per item
    return paths

agent = GeneraticAgent()
agent.verbose = False # 把 Agent 的详细输出模式关掉

# 正则，用于从 Agent 回复里删除内部标签内容
_TAG_PATS = [r'<' + t + r'>.*?</' + t + r'>' for t in ('thinking', 'tool_use')]
_TAG_PATS.append(r'<file_content>.*?</file_content>')

def _strip_md(t):
    """Filter markdown for WeChat rich-text rendering.
    WeChat natively renders: code fences, inline code, bold, italic,
    H1-H4 headings, horizontal rules, tables. We only strip unsupported syntax.
    保留：短代码块、行内代码、加粗、斜体、H1-H4、表格、分割线
    保留：完整代码块
    删除：Markdown 图片
    转换：链接只保留文字
    转换：无序列表改成 •
    清理：H5/H6 标题符号、有序列表编号、引用符号、多余空行
    """
    code_blocks = []

    def _hold_code(m):
        code_blocks.append(m.group(0))
        return f'\x00CODE{len(code_blocks)-1}\x00'

    def _restore_code(m):
        return code_blocks[int(m.group(1))]

    def _format_bullet_lines(text):
        # WeChat rich-text treats ordinary single newlines as soft breaks.
        # Use Markdown hard breaks around bullet lines so bullets stay on separate lines.
        text = re.sub(r'(?<!\n)[ \t]+•[ \t]+', '\n• ', text)
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if line.startswith('• '):
                if i > 0 and lines[i - 1].strip() and not lines[i - 1].endswith('  '):
                    lines[i - 1] += '  '
                lines[i] = line.rstrip() + '  '
        return '\n'.join(lines)

    t = re.sub(r'(`{3,})[\s\S]*?\1', _hold_code, t)
    # inline code: keep (WeChat renders it)
    # bold/italic (*/**/***): keep (WeChat renders it)
    t = re.sub(r'!\[.*?\]\(.*?\)', '', t)                        # images: remove
    t = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', t)              # links: text only
    t = re.sub(r'^#{5,6}\s+', '', t, flags=re.M)                 # H5-H6: strip (H1-H4 kept)
    t = re.sub(r'^\s*[-*+]\s+', '• ', t, flags=re.M)             # unordered list: bullet
    t = _format_bullet_lines(t)
    t = re.sub(r'^\s*\d+\.\s+', '', t, flags=re.M)               # ordered list: strip num
    t = re.sub(r'^\s*>\s?', '', t, flags=re.M)                   # blockquote: strip
    t = re.sub(r'\x00CODE(\d+)\x00', _restore_code, t)
    # horizontal rules (---): keep (WeChat renders it)
    return re.sub(r'\n{3,}', '\n\n', t).strip()

def _clean(t):
    """把 Agent 原始输出清理成适合发给微信用户看的文本"""

    # 删除 Turn 标记
    t = re.sub(r'^\s*LLM Running \(Turn \d+\) \.{3}\s*$', '', t, flags=re.M)
    # 删除工具调用展示行
    t = re.sub(r'^\s*🛠️\s*[A-Za-z_][A-Za-z0-9_]*\(.*$', '', t, flags=re.M)
    #  删除内部标签块
    for p in _TAG_PATS:
        t = re.sub(p, '', t, flags=re.DOTALL)
    # 只删除 summary 标签本身，不删除 summary 内容
    t = re.sub(r'</?summary>', '', t)
    # 再调用 _strip_md 清理 Markdown
    return re.sub(r'\n{3,}', '\n\n', _strip_md(t)).strip()

def _turn_parts(t):
    """文本解析工具函数，
    核心作用是：从一段文本中，
    按固定标记 LLM Running (Turn 数字) ... 分割文本，提取出多轮对话 / 执行片段，
    同时安全保护长代码块不被错误分割。

    示例:
        input:
            系统初始化完成
            **LLM Running (Turn 1)...**
            回答第一轮问题
            **LLM Running (Turn 2)...**
            正在回答第二轮

        output:
            (
                ["系统初始化完成\n", "**LLM Running (Turn 1)...**\n回答第一轮问题\n"],
                "**LLM Running (Turn 2)...**\n正在回答第二轮",
            )
    """
    _ph = [] # 初始化占位符列表，临时存储被保护的长代码块，防止分割时被破坏
    # 匹配 代码块 这类长文本
    safe = re.sub(
        r'`{4,}.*?`{4,}',
        lambda m: (
            _ph.append(m.group(0)),  # 把完整代码块存入列表
            f'\x00PH{len(_ph)-1}\x00')[1],  # 生成占位符：\x00PH数字\x00
        t,
        flags=re.DOTALL
        )
    # 按 LLM 轮次标记分割文本
    parts = re.split(r'(\**LLM Running \(Turn \d+\) \.\.\.\**)', safe)
    # 还原占位符（把代码块放回去）
    parts = [re.sub(r'\x00PH(\d+)\x00', lambda m: _ph[int(m.group(1))], p) for p in parts]
    if len(parts) < 4:
        return [], t
    # 组合每一轮的完整内容
    turns = [
        parts[i] + (parts[i + 1] if i + 1 < len(parts) else '')
        for i in range(1, len(parts), 2)
    ]
    prefix = [parts[0]] if parts[0].strip() else []
    completed_turns = prefix + turns[:-1]
    current_turn = turns[-1]
    return completed_turns, current_turn


def _send_xhs_status(bot, uid, ctx, text):
    """发送 /xhs 命令的状态文本，自动按微信长度限制分片。"""
    for part in _split_text(text):
        bot.send_text(uid, part, context_token=ctx)


def _send_xhs_file(bot, uid, ctx, file_path):
    """发送 /xhs 生成的文件或截图。"""
    if not file_path or not os.path.exists(file_path):
        return
    ext = os.path.splitext(file_path)[1].lower()
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
        bot.send_image(uid, file_path, context_token=ctx)
    else:
        display_name = _wechat_file_display_name(file_path)
        bot.send_file(uid, file_path, context_token=ctx, display_name=display_name)


def on_message(bot, msg):
    text = bot.extract_text(msg).strip() # 用户发来的文字
    uid = msg.get('from_user_id', '') # 微信用户 ID，回复时要发给这个人
    ctx = msg.get('context_token', '') # 微信消息协议里的回复上下文标识，让回复挂到正确的会话或消息上下文

    # 如果用户发了图片/文件/视频，就下载到本地，返回本地路径
    media_paths = _dl_media(msg.get('item_list', []))
    if not text and not media_paths:
        return
    if media_paths: # 把文件路径拼进 prompt: [用户发送文件：/path/to/file]
        media_text = '\n'.join(f'[用户发送文件: {p}]' for p in media_paths)
        text = f'{text}\n{media_text}' if text else media_text
    print(f'[WX] 收到: {text[:80]}', file=sys.__stdout__)

    # Commands
    if text in ('/stop', '/abort'): # 调用 agent.abort() 停止当前任务
        agent.abort()
        bot.send_text(uid, '已停止', context_token=ctx)
        return
    if text.startswith('/llm'): # /llm 或 /llm N：查看或切换当前 LLM
        args = text.split()
        if len(args) > 1:
            try:
                n = int(args[1])
                agent.next_llm(n)
                bot.send_text(uid, f'切换到 [{agent.llm_no}] {agent.get_llm_name()}', context_token=ctx)
            except (ValueError, IndexError):
                bot.send_text(uid, f'用法: /llm <0-{len(agent.list_llms())-1}>', context_token=ctx)
        else:
            lines = [f"{'→' if cur else '  '} [{i}] {name}" for i, name, cur in agent.list_llms()]
            bot.send_text(uid, 'LLMs:\n' + '\n'.join(lines), context_token=ctx)
        return

    if text.startswith('/xhs'):
        def _handle_xhs():
            try:
                sent_screenshots = set()

                def _xhs_status_callback(event, payload):
                    """接收采集插件的进度事件，必要时把截图提前发给用户。"""
                    message = (payload or {}).get('message', '')
                    if message and event in {'queued', 'page_opening', 'intervention_required', 'page_state_changed'}:
                        _send_xhs_status(bot, uid, ctx, message)
                    screenshot_path = (payload or {}).get('screenshot_path', '')
                    if screenshot_path and screenshot_path not in sent_screenshots:
                        sent_screenshots.add(screenshot_path)
                        _send_xhs_file(bot, uid, ctx, screenshot_path)

                _send_xhs_status(bot, uid, ctx, '开始采集小红书帖子数据...')
                result = fetch_xhs_note(
                    text,
                    headless=True,
                    comment_limit=600,
                    status_callback=_xhs_status_callback,
                    intervention_timeout=120,
                )
                payload = result.get('payload') or {}
                quality = payload.get('quality') or {}
                record_id = result.get('record_id', '')
                file_path = result.get('file_path', '')
                comments = quality.get('comment_count_collected', 0)
                status = quality.get('status', '')
                page_state = quality.get('page_state', '')
                warnings = quality.get('warnings') or []
                reply = (
                    '小红书数据采集完成\n\n'
                    f'record_id: {record_id}\n'
                    f'状态: {status} / {page_state}\n'
                    f'评论数: {comments}\n'
                    f'文件: [FILE:{file_path}]'
                )
                if warnings:
                    reply += '\n\n提示:\n' + '\n'.join(f'- {w}' for w in warnings[:5])
                _send_xhs_status(bot, uid, ctx, reply)
                _send_xhs_file(bot, uid, ctx, file_path)

                screenshot_path = quality.get('screenshot_path', '')
                if screenshot_path and screenshot_path not in sent_screenshots:
                    sent_screenshots.add(screenshot_path)
                    _send_xhs_file(bot, uid, ctx, screenshot_path)
            except XhsUrlError as e:
                _send_xhs_status(bot, uid, ctx, f'用法: /xhs <小红书帖子链接>\n错误: {e}')
            except Exception as e:
                print(f'[WX] xhs err: {type(e).__name__}: {e}', file=sys.__stdout__)
                _send_xhs_status(bot, uid, ctx, f'小红书数据采集失败: {type(e).__name__}: {e}')

        threading.Thread(target=_handle_xhs, daemon=True).start()
        return

    def _handle():
        # 如果不是 / 开头，就给用户原文前面加一段提示：如果需要给用户展示文件，在回复里使用 [FILE:filepath]
        if text.startswith('/'):
            prompt = text
        else:
            prompt = (
                "If you need to show files to user, use [FILE:filepath] in your response."
                f"\n\n{text}"
            )

        # 把任务塞进 Agent 的任务队列。
        # put_task() 会返回一个 Queue；微信线程靠读这个 dq 获取 Agent 输出：
        # 微信线程  -> agent.put_task() -> Agent 后台线程处理
        # 微信线程  <- dq.get()         <- Agent 把结果写回队列
        dq = agent.put_task(prompt, source="wechat")
        _typing_stop = threading.Event()

        def _keep_typing():
            ticket = bot.get_typing_ticket(uid, ctx)
            if not ticket:
                return
            while not _typing_stop.is_set():
                try:
                    bot.send_typing(uid, ticket)
                except:
                    pass
                _typing_stop.wait(2.0)

        threading.Thread(target=_keep_typing, daemon=True).start()

        # 初始化状态变量
        result = '' # 最终结果文本
        sent = 0 # 已经发送了多少个完整turn
        mi = 0 # 已经发送了多少条中间消息
        last_send = 0 # 上次发送中间消息的时间

        def _wx_send(text):
            """真正发微信文本"""
            s = text.strip()
            parts = _split_text(s)
            if not parts:
                return False
            for i, part in enumerate(parts, 1):
                t0 = time.time()
                try:
                    bot.send_text(uid, part, context_token=ctx)
                    print(
                        f'[WX] send ok part={i}/{len(parts)} len={len(part)} '
                        f'dt={time.time()-t0:.1f}s',
                        file=sys.__stdout__,
                    )
                except Exception as e:
                    print(
                        f'[WX] send err part={i}/{len(parts)} len={len(part)} '
                        f'dt={time.time()-t0:.1f}s {type(e).__name__}: {e}',
                        file=sys.__stdout__,
                    )
                    return False
            return True

        def _send(show): # 控制中间消息发送频率
            """
            最多发送 9 条中间消息
            单条过长时分片发送，不丢弃内容
            发得越多，后面间隔越长：6 * mi 秒
            """

            nonlocal mi, last_send
            now = time.time()
            if mi >= 9 or not show.strip():
                return False
            if mi and now - last_send < 6 * mi:
                return None
            if _wx_send(show):
                mi += 1
                last_send = time.time()
                return True
            return False

        try:
            try:
                while True:
                    item = dq.get(timeout=300)
                    if 'done' in item:
                        result = item['done']
                        break
                    raw = item.get('next', '')
                    # 用 _turn_parts() 判断哪些 turn 已经完整结束,
                    # 完整结束的 turn 放进 done；当前还在流式生成的最后一段放进 partial
                    done, partial = _turn_parts(raw)
                    if len(done) > sent:
                        merged = _clean('\n\n'.join(done[sent:])) # 发微信前清理 Agent 内部标签
                        print(
                            f'[WX] turns={len(done)}/{len(done)+1} '
                            f'sent={sent} sending={len(done)-sent}',
                            file=sys.__stdout__,
                        )
                        if _send(merged):
                            sent = len(done)
            except queue.Empty:
                result = '[超时]'
        finally:
            _typing_stop.set()

        # 最终结果发送
        done, partial = _turn_parts(result)
        rest = '\n\n'.join(done[sent:] + [partial] + ['\n\n[任务已完成]'])
        if rest.strip():
            _wx_send(_clean(rest))

        # 如果 Agent 生成了文件
        files = re.findall(r'\[FILE:([^\]]+)\]', result)
        bad = {'filepath', '<filepath>', 'path', '<path>', 'file_path', '<file_path>', '...'}
        files = [
            f for f in files
            if f.strip().lower() not in bad
            and (f if os.path.isabs(f) else os.path.join(_TEMP_DIR, f)) not in media_paths
        ]
        for fpath in set(files):
            if not os.path.isabs(fpath):
                fpath = os.path.join(_TEMP_DIR, fpath)
            try:
                if not os.path.exists(fpath):
                    raise FileNotFoundError(f"文件不存在: {fpath}")
                ext = os.path.splitext(fpath)[1].lower()
                if ext in {'.mp4', '.mov', '.m4v', '.webm'}:
                    bot.send_video(uid, fpath, context_token=ctx)
                elif ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
                    bot.send_image(uid, fpath, context_token=ctx)
                else:
                    display_name = _wechat_file_display_name(fpath)
                    bot.send_file(uid, fpath, context_token=ctx, display_name=display_name)
                print(f'[WX] sent media: {fpath}', file=sys.__stdout__)
            except Exception as e:
                print(f'[WX] send media err: {e}', file=sys.__stdout__)

    # on_message() 是微信收消息回调，如果在里面阻塞等待 Agent，微信轮询就会卡住。
    # 所以它开一个后台线程处理这条请求。
    threading.Thread(target=_handle, daemon=True).start()

if __name__ == '__main__':
    try:
        # 创建一个 TCP socket
        #   socket.AF_INET：使用 IPv4
        #   socket.SOCK_STREAM：使用 TCP
        _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock.bind(('127.0.0.1', 19531))
    except OSError:
        print('[WeChat] Another instance running, exiting.')
        sys.exit(1)

    # 把日志写到 temp/wechatapp.log
    _logf = open(
        os.path.join(_TEMP_DIR, 'wechatapp.log'),
        'a',
        encoding='utf-8',
        buffering=1,
    )
    sys.stdout = sys.stderr = _logf
    print(f'[NEW] Process starting {time.strftime("%m-%d %H:%M")}')
    bot = WxBotClient()

    # 如果没有 token，走二维码登录
    if not bot.token:
        sys.stdout = sys.stderr = sys.__stdout__  # restore for QR display
        bot.login_qr()
        sys.stdout = sys.stderr = _logf

    # 启动 agent.run() 后台线程
    threading.Thread(target=agent.run, daemon=True).start()
    print(f'WeChat Bot 已启动 (bot_id={bot.bot_id})', file=sys.__stdout__)
    # 调用 bot.run_loop(on_message) 开始监听微信
    bot.run_loop(on_message)
