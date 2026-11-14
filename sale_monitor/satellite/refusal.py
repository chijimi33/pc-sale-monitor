"""Recognise a refusal or a challenge addressed to the visitor.

A word such as "captcha" or "不正なアクセス" proves nothing: product pages for
routers and security software use them ("Login Captcha", "不正なアクセスを防ぐ").
What counts is a whole block of the page — a title, a heading, a paragraph, a
table cell — that IS a refusal notice or a request to prove one is human, or a
challenge widget in the fetched HTML. Page length never limits the inspection,
and product information elsewhere on the page never cancels a match.

Unknown challenge pages are not guaranteed to be detected.
"""
from __future__ import annotations

import re

# Each pattern must match one whole block after normalisation (case-folded,
# whitespace collapsed, surrounding brackets and punctuation removed). Allowed
# lead-ins and continuations are written into the patterns themselves.
_LEAD = r"(?:(?:http )?(?:error|エラー)?[ :：-]*(?:40[13]|429|503)?[ :：-]*(?:forbidden|error|エラー)?[ :：-]*)?"
_NOTICE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    _LEAD + r"access (?:is |was |has been )?denied",
    _LEAD + r"(?:you have been|your access (?:has been|is|was)|your request (?:has been|was)) (?:blocked|denied)(?: [^.。]{0,80})?",
    _LEAD + r"you (?:do not|don't) have permission to access (?:this (?:page|site|resource|server)|[^ ]{1,200} on this server)",
    r"(?:our systems have |we have |we )?detected unusual traffic(?: from your (?:computer )?network)?(?: .{0,200})?",
    r"are you a (?:robot|human) ?\??",
    r"i(?:'| a)?m not a robot",
    r"(?:please )?(?:verify|confirm|prove) (?:that )?you(?:'re| are) (?:a )?(?:human|not a (?:robot|bot))(?: .{0,120})?",
    r"(?:please )?complete the (?:security check|captcha)(?: to (?:continue|access|proceed)[^.。]{0,80})?",
    r"checking (?:if the site connection is secure|your browser)(?: .{0,80})?",
    _LEAD + r"(?:このページへの|当サイトへの|サイトへの|お客様の)?アクセス(?:が|は)(?:一時的に)?(?:拒否|禁止|制限|ブロック|遮断)され(?:ました|ています|ております)",
    _LEAD + r"(?:このページへの|当サイトへの|サイトへの|お客様の)?アクセスを(?:一時的に)?(?:拒否|制限|ブロック|遮断)(?:しました|しています|しております|させていただきました|させていただいております)",
    r"不正なアクセス(?:を検[知出]しました|が検[知出]されました)(?:.{0,80})?",
    r"(?:私は)?ロボットではありません",
    r"あなたはロボットですか ?[?？]?",
    r"(?:お客様が)?ロボット(?:では|で)ないことを(?:ご)?確認(?:してください|して下さい|ください|下さい|します|させてください|させていただきます)",
    r"(?:お客様が)?人間であることを(?:ご)?確認(?:してください|して下さい|ください|下さい|します|させてください|させていただきます)",
))
_STRIP = " \t\r\n.。!！:：-–—・「」『』【】[]()（）<>《》\"'“”‘’"

_CHALLENGE_CLASSES = ("g-recaptcha", "h-captcha", "cf-turnstile")
_CHALLENGE_FIELDS = ("g-recaptcha-response", "h-captcha-response", "cf-turnstile-response")
_CHALLENGE_XPATH = " | ".join(
    [f'//*[contains(concat(" ", normalize-space(@class), " "), " {name} ")]' for name in _CHALLENGE_CLASSES]
    + [f'//*[@name="{name}"]' for name in _CHALLENGE_FIELDS]
    + ['//iframe[contains(translate(@src, "CAPTCHA", "captcha"), "captcha")]'])
_BLOCKS = frozenset((
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details", "dialog", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "head", "header", "hr", "html", "legend", "li", "main", "nav", "noscript", "ol", "p", "pre", "section",
    "summary", "table", "tbody", "td", "tfoot", "th", "thead", "title", "tr", "ul", "br"))
_INVISIBLE = frozenset(("script", "style", "template"))
_TAGS = re.compile(r"(?is)<!--.*?-->|<(script|style|template)\b.*?</\1\s*>|<[^>]+>")


def _normalise(text):
    return " ".join(text.casefold().split()).strip(_STRIP)


def _is_notice(unit):
    unit = _normalise(unit)
    return bool(unit) and any(pattern.fullmatch(unit) for pattern in _NOTICE_PATTERNS)


def _units(element, out):
    """Collect block-level runs of text; inline markup inside a run is joined."""
    run = [element.text or ""]
    for child in element:
        tag = child.tag if isinstance(child.tag, str) else None
        if tag in _INVISIBLE or tag is None:
            run.append(child.tail or "")
        elif tag in _BLOCKS:
            out.append("".join(run))
            _units(child, out)
            run = [child.tail or ""]
        else:
            inner = []
            _units(child, inner)
            # An inline element contributes to the surrounding run.
            run.append(" ".join(inner))
            run.append(child.tail or "")
    out.append("".join(run))


def refusal_feature(text):
    """Return what makes this response a refusal ("notice" or "challenge_widget"), else None."""
    units, widget = [], False
    if "<" in text:
        try:
            from lxml import html
            tree = html.document_fromstring(text.encode("utf-8"), parser=html.HTMLParser(
                remove_comments=True, remove_pis=True, encoding="utf-8"))
            widget = bool(tree.xpath(_CHALLENGE_XPATH))
            _units(tree, units)
        except Exception:
            # Unparseable markup: fall back to the lines left after removing tags.
            units = _TAGS.sub("\n", text).splitlines()
    else:
        # Plain text (for example an error body or robots.txt): each line, and
        # the whole body when it is a single short statement.
        units = text.splitlines() + [text]
    if any(_is_notice(unit) for unit in units):
        return "notice"
    return "challenge_widget" if widget else None
