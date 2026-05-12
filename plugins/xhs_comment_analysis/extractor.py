"""从已经打开的小红书页面中提取帖子和评论数据。"""

import re
import time
from typing import Any


STATE_AND_DOM_JS = r"""
function safeText(el) {
  return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();
}

function attr(el, name) {
  return el ? (el.getAttribute(name) || '') : '';
}

function numberText(text) {
  if (!text) return 0;
  text = String(text).trim();
  const m = text.match(/([0-9]+(?:\.[0-9]+)?)(万|w|W|k|K)?/);
  if (!m) return 0;
  let n = parseFloat(m[1]);
  const unit = m[2] || '';
  if (unit === '万' || unit === 'w' || unit === 'W') n *= 10000;
  if (unit === 'k' || unit === 'K') n *= 1000;
  return Math.round(n);
}

function uniq(values) {
  return Array.from(new Set((values || []).filter(Boolean)));
}

function pickInitialState() {
  const candidates = [
    window.__INITIAL_STATE__,
    window.__INITIAL_SSR_STATE__,
    window.__NUXT__,
    window.__NEXT_DATA__
  ];
  for (const item of candidates) {
    if (item && typeof item === 'object') return item;
  }
  return null;
}

function findNoteObject(obj, noteId, depth = 0, seen = new Set()) {
  if (!obj || typeof obj !== 'object' || depth > 8 || seen.has(obj)) return null;
  seen.add(obj);
  if (obj.note && typeof obj.note === 'object') {
    const note = obj.note;
    const id = note.note_id || note.noteId || note.id;
    if (!noteId || id === noteId || note.title || note.desc || note.interact_info) return note;
  }
  const id = obj.note_id || obj.noteId || obj.id;
  if ((obj.title || obj.desc || obj.interact_info || obj.user) && (!noteId || id === noteId)) return obj;
  for (const value of Object.values(obj)) {
    const found = findNoteObject(value, noteId, depth + 1, seen);
    if (found) return found;
  }
  return null;
}

function findCommentArrays(obj, depth = 0, seen = new Set(), found = []) {
  if (!obj || typeof obj !== 'object' || depth > 9 || seen.has(obj)) return found;
  seen.add(obj);
  if (Array.isArray(obj)) {
    const score = obj.filter(item => item && typeof item === 'object' && (
      item.comment_id || item.commentId ||
      ((item.content || item.text) && (item.user_info || item.userInfo || item.user || item.owner))
    )).length;
    if (score >= 1) found.push(obj);
    for (const item of obj.slice(0, 30)) findCommentArrays(item, depth + 1, seen, found);
    return found;
  }
  for (const value of Object.values(obj)) {
    findCommentArrays(value, depth + 1, seen, found);
  }
  return found;
}

function noteIdFromUrl() {
  const parts = location.pathname.split('/').filter(Boolean);
  const names = new Set(['explore', 'search_result', 'discovery', 'item']);
  if (parts.length >= 2 && names.has(parts[parts.length - 2])) {
    const last = parts[parts.length - 1] || '';
    return /^[0-9a-zA-Z_-]+$/.test(last) ? last : '';
  }
  const params = new URLSearchParams(location.search);
  const redirectPath = params.get('redirectPath') || '';
  if (redirectPath) {
    const redirectParts = redirectPath.split('?')[0].split('/').filter(Boolean);
    if (redirectParts.length >= 2 && names.has(redirectParts[redirectParts.length - 2])) {
      const last = redirectParts[redirectParts.length - 1] || '';
      return /^[0-9a-zA-Z_-]+$/.test(last) ? last : '';
    }
  }
  return '';
}

function normalizeComment(raw, rootId = null, level = 1) {
  if (!raw || typeof raw !== 'object') return null;
  const user = raw.user_info || raw.userInfo || raw.user || raw.owner || {};
  const targetComment = raw.target_comment || raw.targetComment || raw.reply_comment || raw.replyComment || {};
  const targetUser = targetComment.user_info || targetComment.userInfo || targetComment.user || {};
  const interact = raw.interact_info || raw.interactInfo || {};
  const id = raw.comment_id || raw.commentId || raw.id || '';
  const rootCommentId = level === 1 ? null : rootId;
  const directReplyId = level === 1 ? null : (
    targetComment.comment_id || targetComment.commentId || targetComment.id ||
    raw.reply_comment_id || raw.replyCommentId ||
    raw.target_comment_id || raw.targetCommentId ||
    raw.parent_comment_id || raw.parentCommentId ||
    rootId
  );
  const text = raw.content || raw.text || raw.desc || raw.note || '';
  if (!text && !id) return null;
  const userId = user.user_id || user.userId || user.id || raw.user_id || raw.userId || '';
  return {
    comment_id: id,
    level,
    root_comment_id: rootCommentId || null,
    reply_comment_id: directReplyId || null,
    owner_user_id: userId,
    owner_nickname: user.nickname || user.nick_name || user.name || raw.nickname || '',
    text: text,
    liked_count: numberText(raw.like_count || raw.likeCount || raw.liked_count || raw.likedCount || interact.liked_count || interact.like_count),
    reply_count: numberText(raw.sub_comment_count || raw.subCommentCount || raw.reply_count || raw.replyCount),
    created_at: raw.create_time || raw.createTime || raw.time || raw.created_at || '',
    metadata: {
      source: 'state',
      ip_location: raw.ip_location || raw.ipLocation || '',
      reply_to_user_id: targetUser.user_id || targetUser.userId || targetUser.id || '',
      reply_to_nickname: targetUser.nickname || targetUser.nick_name || targetUser.name || ''
    }
  };
}

function normalizeCommentTree(items) {
  const out = [];
  for (const raw of items || []) {
    if (!raw || typeof raw !== 'object') continue;
    const parent = normalizeComment(raw, null, 1);
    if (parent) out.push(parent);
    const children = raw.sub_comments || raw.subComments || raw.sub_comment_list || raw.subCommentList || raw.replies || raw.reply_list || [];
    if (Array.isArray(children)) {
      const rootId = parent ? parent.comment_id : (raw.comment_id || raw.commentId || raw.id || null);
      for (const child of children) {
        const normalizedChild = normalizeComment(child, rootId, 2);
        if (normalizedChild) out.push(normalizedChild);
      }
    }
  }
  return out;
}

function extractCommentsFromState(state) {
  const arrays = findCommentArrays(state);
  let best = [];
  for (const arr of arrays) {
    const comments = normalizeCommentTree(arr);
    if (comments.length > best.length) best = comments;
  }
  return best;
}

function normalizeNote(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const interact = raw.interact_info || raw.interactInfo || {};
  const user = raw.user || raw.user_info || raw.userInfo || {};
  const tags = raw.tag_list || raw.tagList || raw.hash_tag || raw.hashTags || [];
  return {
    post: {
      post_id: raw.note_id || raw.noteId || raw.id || noteIdFromUrl(),
      title: raw.title || raw.display_title || raw.displayTitle || '',
      content: raw.desc || raw.content || raw.note_card?.desc || '',
      tags: Array.isArray(tags) ? tags.map(t => t.name || t.title || t.tag_name || '').filter(Boolean) : [],
      created_at: raw.time || raw.create_time || raw.createTime || raw.last_update_time || '',
      liked_count: numberText(interact.liked_count || interact.likedCount || interact.like_count || interact.likeCount),
      collected_count: numberText(interact.collected_count || interact.collectedCount || interact.collect_count || interact.collectCount),
      comment_count: numberText(interact.comment_count || interact.commentCount),
      shared_count: numberText(interact.share_count || interact.shareCount),
      metadata: {}
    },
    post_owner: {
      user_id: user.user_id || user.userId || user.id || '',
      nickname: user.nickname || user.nick_name || user.name || '',
      profile_url: user.user_id || user.userId || user.id ? `https://www.xiaohongshu.com/user/profile/${user.user_id || user.userId || user.id}` : '',
      bio: user.desc || user.description || '',
      location: user.ip_location || user.location || '',
      verified: user.red_official_verified || user.verified || null,
      metadata: {}
    }
  };
}

function extractDomPost() {
  const titleEl = document.querySelector('#detail-title, .title, [class*=title]');
  const descEl = document.querySelector('#detail-desc, .desc, [class*=desc]');
  const authorEl = document.querySelector('.author .name, .user-name, [class*=author] [class*=name]');
  return {
    post: {
      post_id: noteIdFromUrl(),
      title: safeText(titleEl),
      content: safeText(descEl),
      tags: uniq(Array.from(document.querySelectorAll('a, span')).map(e => safeText(e)).filter(t => t.startsWith('#')).map(t => t.replace(/^#/, ''))),
      metadata: {}
    },
    post_owner: {
      nickname: safeText(authorEl),
      metadata: {}
    }
  };
}

function looksLikeComment(el) {
  const cls = String(el.className || '').toLowerCase();
  if (cls.includes('comment')) return true;
  const txt = safeText(el);
  return txt.length >= 2 && txt.length < 1000 && /赞|回复|小时前|分钟前|昨天|IP属地|展开/.test(txt);
}

function extractCommentsFromDom() {
  const selectors = [
    '[class*=comment-item]',
    '[class*=CommentItem]',
    '.comment-item',
    '.parent-comment',
    '.comment'
  ];
  let nodes = [];
  for (const selector of selectors) {
    nodes = Array.from(document.querySelectorAll(selector)).filter(looksLikeComment);
    if (nodes.length) break;
  }
  const comments = [];
  for (const el of nodes) {
    const textEl = el.querySelector('[class*=content], [class*=text], .content, .text') || el;
    const authorEl = el.querySelector('a[href*="/user/profile"], [class*=author], [class*=name]');
    const profile = authorEl && authorEl.closest('a') ? authorEl.closest('a') : el.querySelector('a[href*="/user/profile"]');
    const profileUrl = profile ? new URL(attr(profile, 'href'), location.href).href : '';
    const ownerId = (profileUrl.match(/\/user\/profile\/([^/?#]+)/) || [])[1] || '';
    const rawText = safeText(textEl);
    if (!rawText) continue;
    const isChild = Boolean(el.closest('[class*=reply], [class*=sub]')) || /reply|sub/.test(String(el.className || '').toLowerCase());
    comments.push({
      comment_id: attr(el, 'data-id') || attr(el, 'data-comment-id') || '',
      level: isChild ? 2 : 1,
      root_comment_id: null,
      reply_comment_id: null,
      owner_user_id: ownerId,
      owner_nickname: safeText(authorEl),
      text: rawText,
      liked_count: numberText(safeText(el.querySelector('[class*=like], [class*=count]'))),
      reply_count: 0,
      created_at: '',
      metadata: {}
    });
  }
  return comments;
}

function detectPageState() {
  const text = document.body ? document.body.innerText : '';
  const title = document.title || '';
  const lowerUrl = location.href.toLowerCase();
  const combined = text + title + lowerUrl;
  const hasLogin = /登录|扫码|手机号登录|短信登录|请先登录/.test(text + title);
  if (/404|not.?found/i.test(title + text)) return 'not_found';
  if (/ip at risk|error_code=300012|安全限制/i.test(combined)) return 'risk_control';
  if (/安全验证|请通过验证|拖动滑块|人机验证|请完成验证|扫码验证身份|验证身份|二维码\d*分钟失效|verify|captcha/i.test(combined)) return 'verification_required';
  if (/登录|扫码登录|请先登录/.test(text + title)) return 'login_required';
  if (hasLogin) return 'login_or_verification';
  return 'normal';
}

const noteId = noteIdFromUrl();
const state = pickInitialState();
const note = normalizeNote(findNoteObject(state, noteId));
const dom = extractDomPost();
const stateComments = extractCommentsFromState(state);
// state 对象里有结构化评论时，不再混入 DOM 兜底结果。
// DOM 结果通常缺少 comment_id 和作者信息，混合后会把同一条评论重复保存。
const comments = stateComments.length ? stateComments : extractCommentsFromDom();
return {
  page_state: detectPageState(),
  state_found: Boolean(state),
  note: {
    post: Object.assign({}, dom.post, note.post || {}),
    post_owner: Object.assign({}, dom.post_owner, note.post_owner || {})
  },
  comments,
  html_length: document.documentElement.outerHTML.length,
  body_text_length: document.body ? document.body.innerText.length : 0
};
"""


EXPAND_REPLIES_JS = r"""
const patterns = [
  /展开.*回复/,
  /查看.*回复/,
  /更多回复/,
  /还有.*回复/,
  /共.*回复/,
  /条回复/
];
const candidates = [];
function visible(el) {
  const rect = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
for (const el of Array.from(document.querySelectorAll('button, span, div, a'))) {
  const text = (el.innerText || '').trim();
  if (!text || text.length > 40) continue;
  if (!patterns.some(pattern => pattern.test(text))) continue;
  if (!visible(el)) continue;
  candidates.push({el, top: el.getBoundingClientRect().top});
}
candidates.sort((a, b) => a.top - b.top);
let clicked = 0;
for (const item of candidates) {
  try {
    item.el.scrollIntoView({block: 'center', inline: 'nearest'});
    item.el.click();
    clicked++;
    if (clicked >= 3) break;
  } catch (_) {}
}
return {
  clicked,
  candidates: candidates.length
};
"""


SCROLL_JS = r"""
function safeText(el) {
  return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();
}

function scoreContainer(el) {
  const text = safeText(el).slice(0, 3000);
  const cls = String(el.className || '').toLowerCase();
  let score = 0;
  if (/comment|reply|评论|回复/.test(cls)) score += 2000;
  if (/评论|回复|赞|IP属地/.test(text)) score += 1000;
  score += Math.min(el.scrollHeight, 5000);
  return score;
}

const containers = Array.from(document.querySelectorAll('div, main, section')).filter(el => {
  const style = getComputedStyle(el);
  return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 50;
});
const target = containers.sort((a, b) => scoreContainer(b) - scoreContainer(a))[0] || document.scrollingElement || document.documentElement;
const before = target.scrollTop;
const clientHeight = target.clientHeight || window.innerHeight || 900;
const step = Math.max(420, Math.min(760, Math.floor(clientHeight * 0.55)));
target.scrollTop = Math.min(target.scrollTop + step, target.scrollHeight);
return {
  before,
  after: target.scrollTop,
  scrollHeight: target.scrollHeight,
  clientHeight: target.clientHeight,
  step,
  moved: Math.abs(target.scrollTop - before) > 5
};
"""


def _clean_text(value: Any) -> str:
    """清理页面文本，避免空白和换行影响去重。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _comment_key(comment: dict[str, Any]) -> str:
    """生成评论去重键。

    小红书页面不一定稳定暴露 comment_id，因此要用文本、作者和父评论
    关系兜底。
    """
    parts = [
        comment.get("comment_id"),
        comment.get("root_comment_id"),
        comment.get("reply_comment_id"),
        comment.get("owner_user_id"),
        comment.get("owner_nickname"),
        comment.get("text"),
        comment.get("created_at"),
    ]
    return "|".join(_clean_text(p) for p in parts)


def _merge_comments(existing: dict[str, dict[str, Any]], comments: list[dict[str, Any]], limit: int) -> None:
    """把本轮页面可见评论合并到总表。"""
    for comment in comments:
        text = _clean_text(comment.get("text"))
        if not text:
            continue
        comment["text"] = text
        key = _comment_key(comment)
        if key not in existing:
            existing[key] = comment
        if len(existing) >= limit:
            break


def _build_comment_owners(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从评论列表中汇总评论作者基础信息。"""
    owners: dict[str, dict[str, Any]] = {}
    for comment in comments:
        owner_id = _clean_text(comment.get("owner_user_id"))
        nickname = _clean_text(comment.get("owner_nickname"))
        profile_url = f"https://www.xiaohongshu.com/user/profile/{owner_id}" if owner_id else ""
        key = owner_id or profile_url or nickname
        if not key or key in owners:
            continue
        owners[key] = {
            "user_id": owner_id,
            "nickname": nickname,
            "profile_url": profile_url,
            "bio": "",
            "location": "",
            "followers_count": None,
            "following_count": None,
            "liked_count": None,
            "verified": None,
            "profile_tags": [],
            "metadata": {},
        }
    return list(owners.values())


def _quality_status(page_state: str, comment_count: int) -> str:
    """根据页面状态和评论数判断采集状态。"""
    if page_state in {"login_required", "verification_required", "login_or_verification", "risk_control", "not_found"}:
        if comment_count > 0 and page_state != "not_found":
            return "partial_success"
        return "failed"
    if page_state != "normal" or comment_count == 0:
        return "partial_success"
    return "success"


def _safe_int(value: Any) -> int:
    """把页面里可能出现的数字字段稳定转换为 int。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _target_comment_count(declared_count: int, comment_limit: int) -> int:
    """根据帖子评论数计算本次采集目标，最多不超过配置上限。"""
    if declared_count <= 0:
        return comment_limit
    return max(1, min(declared_count, comment_limit))


def _max_collect_rounds(target_count: int) -> int:
    """评论越多，允许越多轮慢速加载。"""
    if target_count <= 80:
        return 35
    if target_count <= 200:
        return 60
    if target_count <= 400:
        return 90
    return 120


def _idle_round_limit(target_count: int) -> int:
    """评论多时给页面更多轮机会，避免网络慢时过早停止。"""
    if target_count <= 80:
        return 6
    if target_count <= 200:
        return 8
    return 10


def _settle_seconds(round_no: int, no_growth_rounds: int, clicked_count: int) -> float:
    """控制采集节奏，宁可慢一点，减少连续高频操作。"""
    seconds = 1.8
    if clicked_count:
        seconds += 0.8
    if no_growth_rounds:
        seconds += min(no_growth_rounds * 0.4, 2.0)
    if round_no and round_no % 12 == 0:
        seconds += 2.0
    return seconds


def _has_more_comments(declared_count: int, collected_count: int, comment_limit: int) -> bool:
    """判断达到停止条件时是否仍可能有未采集评论。"""
    if declared_count > 0:
        return collected_count < declared_count
    return collected_count >= comment_limit


def _emit_progress(callback, **payload: Any) -> None:
    """向前端报告采集进度；回调失败不影响采集。"""
    if not callback:
        return
    try:
        callback("comment_progress", payload)
    except Exception:
        pass


def extract_note_data(tab: Any, *, comment_limit: int = 800, status_callback=None) -> dict[str, Any]:
    """从 DrissionPage tab 中提取原始帖子数据。

    实现原则：
    - 优先读取页面已加载的状态对象，减少对 DOM 结构的依赖。
    - 同时使用 DOM 启发式兜底，因为不同小红书页面入口结构会有差异。
    - 评论采用“滚动一段、采集一轮、合并去重”的方式，避免虚拟列表丢数据。
    """
    warnings: list[str] = []
    errors: list[str] = []
    comments_by_key: dict[str, dict[str, Any]] = {}
    post: dict[str, Any] = {}
    post_owner: dict[str, Any] = {}
    page_state = "unknown"
    has_more_comments = None
    target_count = comment_limit
    max_rounds = _max_collect_rounds(target_count)
    idle_limit = _idle_round_limit(target_count)
    next_progress_threshold = 100

    try:
        tab.wait.doc_loaded(timeout=20)
    except Exception as exc:
        warnings.append(f"页面加载等待超时: {exc}")

    # 多轮滚动采集。连续几轮没有新增评论后停止，避免无意义地滚动。
    no_growth_rounds = 0
    round_no = 0
    while round_no < max_rounds:
        try:
            result = tab.run_js(STATE_AND_DOM_JS, timeout=12)
        except Exception as exc:
            errors.append(f"页面数据提取失败: {exc}")
            break

        if isinstance(result, dict):
            page_state = result.get("page_state") or page_state
            note = result.get("note") or {}
            post.update(note.get("post") or {})
            post_owner.update(note.get("post_owner") or {})
            declared_count = _safe_int(post.get("comment_count"))
            if declared_count:
                target_count = _target_comment_count(declared_count, comment_limit)
                max_rounds = _max_collect_rounds(target_count)
                idle_limit = _idle_round_limit(target_count)
            before_count = len(comments_by_key)
            _merge_comments(comments_by_key, result.get("comments") or [], comment_limit)
            collected_count = len(comments_by_key)
            if len(comments_by_key) == before_count:
                no_growth_rounds += 1
            else:
                no_growth_rounds = 0
            if collected_count >= next_progress_threshold:
                level1_count = sum(1 for comment in comments_by_key.values() if int(comment.get("level") or 1) == 1)
                level2_count = sum(1 for comment in comments_by_key.values() if int(comment.get("level") or 1) == 2)
                _emit_progress(
                    status_callback,
                    message=f"正在采集评论: {collected_count}/{target_count}，一级 {level1_count}，二级 {level2_count}",
                    collected_count=collected_count,
                    target_count=target_count,
                    level1_comment_count_collected=level1_count,
                    level2_comment_count_collected=level2_count,
                    round_no=round_no,
                )
                next_progress_threshold = (collected_count // 100 + 1) * 100
        else:
            warnings.append("页面提取脚本没有返回字典结果")

        if page_state in {"login_required", "verification_required", "login_or_verification", "risk_control", "not_found"}:
            break
        if len(comments_by_key) >= target_count:
            has_more_comments = _has_more_comments(_safe_int(post.get("comment_count")), len(comments_by_key), comment_limit)
            break
        if no_growth_rounds >= idle_limit and round_no >= idle_limit:
            has_more_comments = False
            break

        # 展开二级评论后再滚动。点击失败不影响主流程。
        clicked_count = 0
        try:
            expand_result = tab.run_js(EXPAND_REPLIES_JS, timeout=5)
            if isinstance(expand_result, dict):
                clicked_count = _safe_int(expand_result.get("clicked"))
            else:
                clicked_count = _safe_int(expand_result)
            if clicked_count:
                time.sleep(1.2)
        except Exception:
            pass

        try:
            scroll_result = tab.run_js(SCROLL_JS, timeout=5)
            if isinstance(scroll_result, dict) and not scroll_result.get("moved"):
                no_growth_rounds += 1
            time.sleep(_settle_seconds(round_no, no_growth_rounds, clicked_count))
        except Exception as exc:
            warnings.append(f"评论区滚动失败: {exc}")
            break
        round_no += 1

    comments = list(comments_by_key.values())[:comment_limit]
    comment_owners = _build_comment_owners(comments)
    level1_count = sum(1 for comment in comments if int(comment.get("level") or 1) == 1)
    level2_count = sum(1 for comment in comments if int(comment.get("level") or 1) == 2)
    declared_comment_count = _safe_int(post.get("comment_count"))
    if page_state == "normal" and has_more_comments is None:
        has_more_comments = _has_more_comments(declared_comment_count, len(comments), comment_limit)
    if page_state in {"login_required", "verification_required", "login_or_verification", "risk_control"} and comments:
        if declared_comment_count > 0:
            has_more_comments = _has_more_comments(declared_comment_count, len(comments), comment_limit)
        else:
            has_more_comments = True
        warnings.append(f"采集中页面进入 {page_state} 状态，已停止继续滚动并保存已采集的 {len(comments)} 条评论")
    if page_state == "normal" and declared_comment_count > len(comments):
        has_more_comments = True
        warnings.append(f"页面显示评论总数 {declared_comment_count}，本次采集 {len(comments)} 条，仍有评论未采集")
        if round_no >= max_rounds:
            warnings.append(f"已达到本次慢速加载上限 {max_rounds} 轮，为降低风控风险停止继续滚动")

    missing_fields = []
    for field in ("post_id", "title", "content"):
        if not _clean_text(post.get(field)):
            missing_fields.append(f"post.{field}")
    if not _clean_text(post_owner.get("nickname")):
        missing_fields.append("post_owner.nickname")

    status = _quality_status(page_state, len(comments))
    if page_state == "risk_control":
        warnings.append("页面触发小红书安全限制，通常需要更换网络、登录态或人工验证后重试")
    elif page_state == "verification_required":
        warnings.append("页面要求扫码或验证码验证，已保存当前已采集数据")
    elif page_state == "login_required":
        warnings.append("页面要求重新登录，已保存当前已采集数据")
    elif page_state == "login_or_verification":
        warnings.append("页面需要登录或验证，已保存截图供用户处理")
    elif not comments:
        warnings.append("未采集到评论，可能是页面未登录、评论区未加载或 DOM 结构变化")

    return {
        "post": post,
        "post_owner": post_owner,
        "comments": comments,
        "comment_owners": comment_owners,
        "quality": {
            "status": status,
            "page_state": page_state,
            "comment_count_collected": len(comments),
            "level1_comment_count_collected": level1_count,
            "level2_comment_count_collected": level2_count,
            "has_more_comments": has_more_comments,
            "missing_fields": missing_fields,
            "warnings": warnings,
            "errors": errors,
        },
    }
