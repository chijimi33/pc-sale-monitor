"""Recognize the retained Sofmap query shell, without fetching or running JS.

This deliberately supports one observed loader contract, not arbitrary JavaScript
or search filters. A changed contract needs new retained evidence. Recognition
only creates an acquisition dependency; it says nothing about products or prices.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from lxml import etree, html

from .http import FetchError
from .parsing import canonical, clean

MAX_BODY = 2 * 1024 * 1024
MAX_SCRIPT = 128 * 1024
MAX_TASKS = 128
MAX_URL = 4096
MAX_QUERY = 256
HOSTS = {'www.sofmap.com', 'sofmap.com'}
SEARCH_PATH = '/search_result.aspx'
PARTS_PATHS = {'/product_list_parts.aspx', '//product_list_parts.aspx'}
TAB_SELECTOR = 'section#search_result_area ul.tab_list li a'

# Contiguous excerpts of the inline script in the two 2026-10-04 retained
# search_result.aspx receipts. Comments/formatting may vary; code may not.
_BINDING = '''
jQuery("section#search_result_area ul.tab_list li a").each(function(){
    jQuery(this).click(function(e){
        e.preventDefault();
        var strTargetUrl = jQuery(this).attr("href");
        document.cookie = 'ptag='+jQuery(this).attr("name");
        if(strTargetUrl !='' && strTargetUrl != '#') {
            GetSearchParts(strTargetUrl,strLoadingArea);
'''
_SELECT_ALL = '''
if(jQuery(this).attr("name") == strProductType) { jQuery(this).click(); }
'''
_PRELUDE = r'''
if(moving) { return; } else { moving = true }
var tmpStrUrl = strUrl.split('?');
var temp_params = tmpStrUrl[1].replace(/^\?/, '').split('&');
var urlParams = {};
for (key in temp_params) {
    var item = temp_params[key].split('=');
    if (item[0] in urlParams) {
        urlParams[item[0]] = urlParams[item[0]] + ',' + item[1];
    } else { urlParams[item[0]] = item[1]; }
}
if(!urlParams['is_page']) {
    strUrl += (strUrl.split('?')[1] ? '&':'?') + 'is_page=serch_result';
}
var retValue = true;
jQuery.ajax(
'''
_BEFORE_SEND = '''function() {
    CreateLoadingFlame(strTargetFlame);
    if(strUrl && strUrl !='' && strUrl != '#') {
        document.cookie = 'rparam='+encodeURIComponent(document.location.search);
        document.cookie = 'pparam='+encodeURIComponent(strUrl);
    }
}'''
_AFTER_LOAD = '''
GetSearchParts(strTargetUrl,strLoadingArea);
if(isFirst) { $('#pgtop').click(); isFirst = false; }
'''
# A small lexer, not an interpreter. Strings stay single tokens, so apparent
# code inside strings/comments cannot prove an executable loader contract.
_TOKEN = re.compile(r'''\s+|//[^\r\n]*|/\*[\s\S]*?\*/|<!--[^\r\n]*|"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[A-Za-z_$][\w$]*|\d+|[^\s]''')


def _tokens(source):
    result = []
    for match in _TOKEN.finditer(source):
        token = match.group()
        if token.isspace() or token.startswith(('//', '/*', '<!--')):
            continue
        if token == '`' or token == '*' and result[-1:] == ['/']:
            raise ValueError('Deferred loader has unsupported or incomplete JS syntax')
        if token[0] in '\"\'':
            if len(token) < 2 or token[-1] != token[0]:
                raise ValueError('Deferred loader has an unterminated JS string')
            token = ('string', token[1:-1])
        result.append(token)
    return result


def _contains(tokens, snippet):
    needle = _tokens(snippet)
    return any(tokens[i:i + len(needle)] == needle for i in range(len(tokens) - len(needle) + 1))


def _end(tokens, start):
    """Return the index after a balanced group; strings are opaque."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    stack = []
    for i in range(start, len(tokens)):
        token = tokens[i]
        if token in pairs:
            stack.append(pairs[token])
        elif token in (')', ']', '}'):
            if not stack or stack.pop() != token:
                raise ValueError('Deferred loader has unbalanced JS delimiters')
            if not stack:
                return i + 1
    raise ValueError('Deferred loader has incomplete JS')


def _ajax_properties(tokens):
    properties = {}
    i = 1  # The containing object has already been balanced.
    while i < len(tokens) - 1:
        key = tokens[i]
        if not isinstance(key, str) or tokens[i + 1:i + 2] != [':'] or key in properties:
            raise ValueError('Deferred loader has ambiguous AJAX properties')
        i += 2
        start = i
        while i < len(tokens) - 1 and tokens[i] != ',':
            i = _end(tokens, i) if tokens[i] in ('(', '[', '{') else i + 1
        properties[key] = tokens[start:i]
        i += 1
    return properties


def _loader(tree):
    scripts = tree.xpath('//script[not(@src)]')
    relevant = [s for s in scripts if any(name in (s.text or '') for name in ('GetSearchParts', 'isFirst'))]
    if sum(len(s.text or '') for s in relevant) > MAX_SCRIPT:
        raise ValueError('Deferred loader exceeds the script limit')
    definitions = []
    initial_declarations = 0
    after_load = _tokens(_AFTER_LOAD)
    assignment_offset = len(after_load) - len(_tokens('isFirst = false; }'))
    for script in relevant:
        if script.get('type', '').lower() not in ('', 'text/javascript', 'application/javascript'):
            raise ValueError('Deferred loader is not an inline executable script')
        tokens = _tokens(script.text or '')
        depth = 0
        for i, token in enumerate(tokens):
            if token == 'isFirst':
                if (not depth and tokens[max(0, i - 1):i + 4] == _tokens('var isFirst = true;')
                        and (i == 1 or tokens[i - 2] == ';')):
                    initial_declarations += 1
                elif (depth and i >= assignment_offset
                      and tokens[i - assignment_offset:i - assignment_offset + len(after_load)] == after_load):
                    # Both retained false assignments follow a synchronous
                    # GetSearchParts call in click handlers. They cannot change
                    # the GET data for that initial call.
                    pass
                elif tokens[max(0, i - 2):i + 2] == _tokens('if(isFirst)'):
                    pass
                elif tokens[max(0, i - 2):i + 2] == _tokens("'isFirst': isFirst }"):
                    pass
                else:
                    raise ValueError('Deferred loader has ambiguous isFirst initialization or writes')
            elif token == ('string', 'isFirst') and tokens[i + 1:i + 3] != [':', 'isFirst']:
                raise ValueError('Deferred loader has an indirect isFirst reference')
            if token == 'GetSearchParts':
                if tokens[max(0, i - 1):i] == ['function']:
                    if depth or tokens[i + 1:i + 7] != ['(', 'strUrl', ',', 'strTargetFlame', ')', '{']:
                        raise ValueError('Deferred loader has an unsupported function definition')
                    end = _end(tokens, i + 6)
                    definitions.append((tokens, tokens[i + 7:end - 1], script.text))
                elif tokens[i + 1:i + 2] != ['(']:
                    raise ValueError('Deferred loader is reassigned or indirect')
            if token == '{':
                depth += 1
            elif token == '}':
                depth -= 1
                if depth < 0:
                    raise ValueError('Deferred loader has malformed JS')
        if depth:
            raise ValueError('Deferred loader has incomplete JS')
    if len(definitions) != 1:
        raise ValueError('Deferred loader must have one GetSearchParts definition')
    if initial_declarations != 1:
        raise ValueError('Deferred loader needs one top-level var isFirst = true declaration')
    tokens, body, source = definitions[0]
    for snippet in ('var strLoadingArea = "#search_result_area";',
                    'var strProductType = "ALL";', _BINDING, _SELECT_ALL):
        if not _contains(tokens, snippet):
            raise ValueError('Deferred loader does not connect the ALL tab to the result area')
    prefix = _tokens(_PRELUDE)
    if body[:len(prefix)] != prefix or body[len(prefix):len(prefix) + 1] != ['{']:
        raise ValueError('Deferred loader URL construction differs from the retained contract')
    end = _end(body, len(prefix))
    if body[end:] != _tokens('); return retValue;'):
        raise ValueError('Deferred loader has additional or missing request code')
    properties = _ajax_properties(body[len(prefix):end])
    expected = {'url': 'strUrl', 'type': '"GET"', 'dataType': '"html"',
                'timeout': '60000', 'cache': 'false',
                'contentType': '"application/x-www-form-urlencoded; charset=Shift_JIS"',
                'data': "{ 'isFirst': isFirst }", 'beforeSend': _BEFORE_SEND}
    if set(properties) - set(expected) - {'success', 'error', 'complete'}:
        raise ValueError('Deferred loader has unsupported AJAX options')
    if any(properties.get(key) != _tokens(value) for key, value in expected.items()):
        raise ValueError('Deferred loader AJAX request differs from the retained GET/html contract')
    for key in {'success', 'error', 'complete'} & properties.keys():
        callback = properties[key]
        if callback[:2] != ['function', '(']:
            raise ValueError('Deferred loader has an indirect AJAX callback')
        end_args = _end(callback, 1)
        if callback[end_args:end_args + 1] != ['{'] or _end(callback, end_args) != len(callback):
            raise ValueError('Deferred loader has a malformed AJAX callback')
    return {'function': 'GetSearchParts', 'method': 'GET', 'data_type': 'html',
            'url_argument': 'strUrl', 'tab_selector': TAB_SELECTOR,
            'append_if_absent': {'is_page': 'serch_result'},
            'initial_declaration': 'var isFirst = true;', 'get_data': {'isFirst': True},
            'omitted_volatile_query': ['_'],
            'request_scope': 'static_initial_GET_dependency_without_browser_state',
            'script_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
            'inspection': 'static_source_only'}


def _url(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL
            or re.search(r'[\s\\\x00-\x1f\x7f]', value) or '#' in value):
        raise ValueError('Deferred search has an invalid URL')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.hostname not in HOSTS or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443)
            or parts.netloc.lower() not in (parts.hostname, parts.hostname + ':443')):
        raise ValueError('Deferred search requires the configured Sofmap HTTPS origin')
    return parts


def _query(parts, *, fragment=False):
    if re.search(r'%(?![0-9a-fA-F]{2})', parts.query):
        raise ValueError('Deferred search has malformed query encoding')
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                      encoding='utf-8', errors='strict', max_num_fields=4)
    values = dict(pairs)
    allowed = {'keyword', 'is_page'} if fragment else {'keyword'}
    query = values.get('keyword', '')
    if (len(values) != len(pairs) or set(values) - allowed or not query
            or any(field.split('=', 1)[0] not in allowed for field in parts.query.split('&'))
            or len(query) > MAX_QUERY or query.strip() != query
            or any(ord(c) < 32 or ord(c) == 127 for c in query)
            or ('is_page' in values and values['is_page'] != 'serch_result')):
        raise ValueError('Deferred search has an ambiguous or unsupported query')
    return query, values


def _class(name):
    return 'contains(concat(" ",normalize-space(@class)," ")," ' + name + ' ")'


class _SourceTitles(HTMLParser):
    """Inspect explicit title boundaries before libxml's platform-specific repair."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active, self.invalid, self.parts, self.titles = False, False, [], []

    def handle_starttag(self, tag, attrs):
        if self.active:
            self.invalid = True
        if tag == 'title':
            self.active, self.parts = True, []

    def handle_endtag(self, tag):
        if tag == 'title':
            if not self.active:
                self.invalid = True
            self.titles.append(''.join(self.parts).strip())
            self.active = False
        elif self.active:
            self.invalid = True

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_comment(self, data):
        if self.active:
            self.invalid = True


def deferred_url(page, query, cfg) -> tuple[str, dict] | None:
    """Return the verified first fragment URL and evidence for an exact query.

    Invalid/ambiguous deferred-looking pages raise ValueError, including bounds.
    cfg is the per-store config (seed_urls anchors the HTTPS host). Only the
    retained unfiltered keyword search and selected ALL tab are supported.
    Inputs are never mutated; attempts, completion and acquisition permissions
    belong to the caller. No cookies, JavaScript, network or filesystem are used.
    """
    # Bound parsing even when handed an unrelated response.
    if len(page.body) > MAX_BODY:
        raise ValueError('Deferred listing body exceeds the inspection limit')
    text = page.text
    raw_titles = _SourceTitles()
    raw_titles.feed(text)
    raw_titles.close()
    if (any(marker in text for marker in ('search_result_area', 'GetSearchParts', 'product_list_parts.aspx'))
            and (raw_titles.active or raw_titles.invalid)):
        # A parser may consume the rest of a malformed document as title text,
        # making all deferred markers disappear from its recovered tree.
        raise ValueError('Deferred search source has malformed title boundaries')
    parser = html.HTMLParser(no_network=True)
    try:
        tree = html.fromstring(text, base_url=page.url, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        if any(marker in text for marker in ('search_result_area', 'GetSearchParts', 'product_list_parts.aspx')):
            raise ValueError('Deferred search HTML is malformed') from exc
        return None
    roots = tree.xpath('//*[@id="search_result_area"]')
    marker = tree.xpath('//a[contains(@href,"product_list_parts.aspx")] | //script[contains(.,"GetSearchParts")]')
    if not roots and not marker:
        return None
    # The retained pages use HTML5 tags and bare ampersands in links, which
    # libxml reports too. Only reject ambiguous attributes or repaired nesting;
    # literal close tags inside legacy scripts are present in the real capture.
    if any(error.type_name == 'ERR_ATTRIBUTE_REDEFINED'
           or error.type_name == 'ERR_TAG_NAME_MISMATCH' and error.message != 'Element script embeds close tag'
           for error in parser.error_log):
        raise ValueError('Deferred search HTML has ambiguous attributes or malformed nesting')
    if tree.xpath('//base[@href]'):
        raise ValueError('Deferred search has an unsupported base URL')
    if len(roots) != 1 or roots[0].tag != 'section':
        raise ValueError('Deferred search needs one result section')
    root = roots[0]
    lists = tree.xpath('//*[@id="change_style_list"]')
    if (len(lists) != 1 or lists[0].tag != 'ul' or root not in lists[0].iterancestors()
            or 'product_list' not in lists[0].get('class', '').split()):
        raise ValueError('Deferred search needs one product list shell')
    if len(lists[0]) or (lists[0].text or '').strip():
        # A normal populated search response is handled by the existing parser.
        return None
    if page.status != 200:
        raise ValueError('Deferred search needs a successful shell response')
    source = _url(page.url)
    if source.path != SEARCH_PATH:
        raise ValueError('Deferred search source path is not search_result.aspx')
    url_query, _ = _query(source)
    if query != url_query:
        raise ValueError('Deferred search does not match the requested query')
    if not isinstance(cfg, dict):
        raise ValueError('Deferred search needs per-store configuration')
    seeds = cfg.get('seed_urls', [])
    if not isinstance(seeds, (list, tuple)) or not 1 <= len(seeds) <= MAX_TASKS:
        raise ValueError('Deferred search needs configured HTTPS seed URLs')
    if not any((_url(seed).hostname, _url(seed).port or 443) == (source.hostname, source.port or 443)
               for seed in seeds):
        raise ValueError('Deferred search does not match the configured host')
    titles = tree.xpath('//title')
    expected_title = query + 'の検索結果｜新品・中古・買取りのソフマップ[sofmap]'
    if (raw_titles.active or raw_titles.invalid or raw_titles.titles != [expected_title]
            or len(titles) != 1 or ''.join(titles[0].itertext()).strip() != expected_title):
        raise ValueError('Deferred search title does not prove the exact query')
    fields = tree.xpath('//input[@name="keyword"]')
    forms = root.xpath('.//form[.//input[@name="keyword"]]')
    if (len(forms) != 1 or not fields or any(f.get('value') != query for f in fields)
            or len(forms[0].xpath('.//input[@name="keyword"]')) != 1
            or forms[0].get('method', 'get').lower() != 'get'):
        raise ValueError('Deferred search form does not prove the exact query')
    action = _url(urljoin(page.url, forms[0].get('action', '')))
    if (action.hostname != source.hostname or action.path != SEARCH_PATH or action.query
            or (action.port or 443) != (source.port or 443)):
        raise ValueError('Deferred search form has a different destination')
    tabs = root.xpath('.//ul[' + _class('tab_list') + ']')
    all_links = root.xpath('.//a[@name="ALL"]')
    selected = tabs[0].xpath('.//a[' + _class('current') + ']') if len(tabs) == 1 else []
    if len(all_links) != 1 or len(selected) != 1 or selected[0] is not all_links[0]:
        raise ValueError('Deferred search needs one unambiguous selected ALL link')
    href = all_links[0].get('href', '')
    target = _url(href)
    if (target.hostname != source.hostname or (target.port or 443) != (source.port or 443)
            or target.path not in PARTS_PATHS):
        raise ValueError('Deferred fragment has a different origin or path')
    target_query, values = _query(target, fragment=True)
    if target_query != query:
        raise ValueError('Deferred fragment keyword does not match the source query')
    loader = _loader(tree)
    url = (href if 'is_page' in values else href + '&is_page=serch_result') + '&isFirst=true'
    evidence = {'kind': 'sofmap_deferred_search_fragment', 'source_url': page.url,
                'fragment_url': url, 'observed_at': page.observed_at,
                'anchor': {'name': 'ALL', 'class': 'current', 'href': href},
                'query': {'requested': query, 'url_keyword': query,
                          'form_keyword': query, 'title': ''.join(titles[0].itertext()).strip()},
                'loader': loader}
    evidence['content_hash'] = hashlib.sha256(page.body).hexdigest()
    return url, evidence


def request_query(url):
    """Read an explicit search URL without dropping any filter parameters."""
    try:
        parts = _url(url)
        if re.search(r'%(?![0-9a-fA-F]{2})', parts.query):
            raise ValueError('Malformed query encoding')
        if parts.path not in PARTS_PATHS | {SEARCH_PATH, '/product_list.aspx'}:
            raise ValueError('Unexpected search path')
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=32)
        values = dict(pairs)
        query = values.get('keyword', '')
        if len(values) != len(pairs) or not query.strip() or len(query) > MAX_QUERY:
            raise ValueError('Ambiguous search query')
        return query
    except (ValueError, TypeError, UnicodeError) as exc:
        raise FetchError('comparison_search_request_unverified') from exc


def parse_listing(page, query):
    """Read displayed result cards and all linked result dependencies.

    Search prices are never observations. Unknown/empty responses stay pending;
    no retained Sofmap response currently proves a zero-result contract.
    """
    try:
        if page.status != 200 or len(page.body) > MAX_BODY or request_query(page.url) != query:
            raise ValueError('Search response does not match the request')
        location = _url(page.url)
        values = dict(parse_qsl(location.query, keep_blank_values=True))
        expected = {'keyword': query}
        if location.path in PARTS_PATHS:
            expected.update(is_page='serch_result', isFirst='true')
        # Other filters/pages are retained as dependencies until their response
        # contract is verified. A matching keyword cannot prove those filters.
        if values != expected or location.path not in PARTS_PATHS | {SEARCH_PATH}:
            raise ValueError('Search filter/page scope is not verified')
        parser = html.HTMLParser(no_network=True)
        tree = html.fromstring(page.text, base_url=page.url, parser=parser)
        if tree.xpath('//base[@href]') or any(e.type_name == 'ERR_ATTRIBUTE_REDEFINED' for e in parser.error_log):
            raise ValueError('Ambiguous result HTML')
        roots = tree.xpath('//*[@id="change_style_list"]')
        if len(roots) != 1 or roots[0].tag != 'ul' or 'product_list' not in roots[0].get('class', '').split():
            raise ValueError('Missing result list')
        forms = tree.xpath('//section[' + _class('list_settings') + ']//form')
        if len(forms) != 1:
            raise ValueError('Missing result query form')
        fields = forms[0].xpath('.//input[@name="keyword"]')
        if len(fields) != 1 or fields[0].get('value') != query:
            raise ValueError('Result query mismatch')
        cards = roots[0].xpath('./li')
        if not cards:
            raise ValueError('Empty results are not verified')
        counts = tree.xpath('//p[' + _class('pg_number_set') + ']/span')
        if not counts or any(not re.fullmatch(r'\d[\d,]*', clean(n)) for n in counts):
            raise ValueError('Missing displayed result count')
        totals = {int(clean(n).replace(',', '')) for n in counts}
        if len(totals) != 1 or next(iter(totals)) < len(cards):
            raise ValueError('Conflicting result counts')
        products, seen = [], set()
        for card in cards:
            links = card.xpath('.//a[' + _class('product_name') + '][@href]')
            urls = {canonical(urljoin(page.url, a.get('href'))) for a in links}
            if len(urls) != 1:
                raise ValueError('Ambiguous result product')
            url = urls.pop()
            target = _url(url)
            params = parse_qsl(target.query, keep_blank_values=True)
            if (target.hostname != urlsplit(page.url).hostname or target.path != '/product_detail.aspx'
                    or len(params) != 1 or params[0][0] != 'sku' or not re.fullmatch(r'\d+', params[0][1])
                    or url in seen or not all(clean(a) for a in links)):
                raise ValueError('Unsupported result product')
            seen.add(url)
            products.append({'url': url, 'title': clean(links[0]), 'source': page.url, 'kind': 'comparison'})
        pages, groups = set(), set()
        for a in tree.xpath('//ol[' + _class('paging_bl') + ']//a[@href]'):
            href = a.get('href', '')
            if not href or href == '#':
                continue
            url = canonical(urljoin(page.url, href))
            if request_query(url) != query or urlsplit(url).hostname != urlsplit(page.url).hostname:
                raise ValueError('Pagination changed query or origin')
            if url != canonical(page.url):
                pages.add(url)
        # Grouped used/new variants are separate search dependencies. Keep their
        # exact filters even when their loader contract is not supported yet.
        for a in roots[0].xpath('.//div[' + _class('used_box') + ']//a[@href]'):
            url = canonical(urljoin(page.url, a.get('href')))
            request_query(url)
            if urlsplit(url).hostname != urlsplit(page.url).hostname:
                raise ValueError('Grouped result changed origin')
            groups.add(url)
        if next(iter(totals)) > len(cards) and not pages:
            raise ValueError('Remaining results have no pagination link')
        dependencies = [{'url': url, 'query': request_query(url)} for url in sorted(pages | groups)]
        evidence = {'query': query, 'result': 'results', 'returned_count': len(products),
                    'displayed_result_count': next(iter(totals)), 'pagination': sorted(pages),
                    'group_dependencies': sorted(groups), 'url': page.url,
                    'observed_at': page.observed_at, 'http_status': page.status,
                    'content_hash': hashlib.sha256(page.body).hexdigest(),
                    'method': 'sofmap_result_cards'}
        return products, dependencies, evidence
    except (ValueError, TypeError, UnicodeError, etree.ParserError, FetchError) as exc:
        raise FetchError('comparison_search_response_unverified') from exc
