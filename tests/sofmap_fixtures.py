"""Reduced excerpts of retained 2026-10-04 Sofmap query HTML.
Source bodies: f067eb1a90c05c042680e888d106a132ec4af9d5e55293a2b9d6eb77e950eab3
and f65a04805a5483970ccbf4c6d8db42151113260e1ee8107ecb1a3bd1a8c17a34.
Navigation and loader UI callbacks are omitted; no network is used by tests.
"""
from html import escape
from urllib.parse import quote
from sale_monitor.http import Page

JAN = '0195553309745'

OTHER_JAN = '4711289500124'

HOST = 'https://www.sofmap.com'

CFG = {'seed_urls': [HOST + '/contents/?id=2959&sid=1'],
       'product_patterns': [r'/product_detail.aspx\?']}

OBSERVED = '2026-10-03T22:33:43.802470+00:00'

SCRIPT = r'''
<!--
var strLoadingArea = "#search_result_area";
var isFirst = true;
$(document).ready(function(){
    var strProductType="ALL";
    jQuery("section#search_result_area ul.tab_list li a").each(function(){
        jQuery(this).click(function(e){
            e.preventDefault();
            var strTargetUrl = jQuery(this).attr("href");
            document.cookie = 'ptag='+jQuery(this).attr("name");
            if(strTargetUrl !='' && strTargetUrl != '#') {
                GetSearchParts(strTargetUrl,strLoadingArea);
                if(isFirst) {
                    $('#pgtop').click();
                    isFirst = false;
                }
            }
        });
        if(jQuery(this).attr("name") == strProductType) {
            jQuery(this).click();
        }
    });
});
function GetSearchParts(strUrl,strTargetFlame)
{
    if(moving) { return; } else { moving = true }
    var tmpStrUrl = strUrl.split('?');
    var temp_params = tmpStrUrl[1].replace(/^\?/, '').split('&');
    var urlParams = {};
    for (key in temp_params) {
        var item = temp_params[key].split('=');
        if (item[0] in urlParams) {
            urlParams[item[0]] = urlParams[item[0]] + ',' + item[1];
        }
        else {
            urlParams[item[0]] = item[1];
        }
    }
    if(!urlParams['is_page']) {
        strUrl += (strUrl.split('?')[1] ? '&':'?') + 'is_page=serch_result';
    }
    var retValue = true;
    jQuery.ajax({
        url: strUrl,
        timeout: 60000,
        dataType: "html",
        type:"GET",
        cache: false,
        contentType: "application/x-www-form-urlencoded; charset=Shift_JIS",
        data: { 'isFirst' : isFirst } ,
        beforeSend: function()
        {
            CreateLoadingFlame(strTargetFlame);
            if(strUrl && strUrl !='' && strUrl != '#') {
                document.cookie = 'rparam='+encodeURIComponent(document.location.search);
                document.cookie = 'pparam='+encodeURIComponent(strUrl);
                //GetSearchParts(strUrl,strLoadingArea);
            }
        },
        success: function(data, status) {
            jQuery("ul.product_list").remove();
            jQuery("div.list-interface-bar").after(data);
        },
        complete: function(XMLHttpRequest, status) { moving = false; }
    });
    return retValue;
}
//-->
'''

def shell(query=JAN, *, href=None, script=SCRIPT, contents='\n\t'):
    """Keep the observed title, search form, tab scope and empty list structure."""
    encoded = quote(query, safe='')
    href = href if href is not None else HOST + '//product_list_parts.aspx?keyword=' + encoded
    return f'''<!doctype html><html lang="ja"><head>
    <title>{escape(query)}の検索結果｜新品・中古・買取りのソフマップ[sofmap]</title>
    </head><body><form action="/search_result.aspx" method="get">
    <input id="searchText" name="keyword" value="{escape(query, quote=True)}"></form>
    <section id="search_result_area">
    <ul class="tab_list col3">
    <li><a name="ALL" class="current" href="{escape(href, quote=True)}">全ての商品<span>( - 点)</span></a></li>
    <li><a name="NEW" href="{HOST}//product_list_parts.aspx?product_type=NEW&amp;keyword={encoded}">新品商品</a></li>
    <li><a name="USED" href="{HOST}//product_list_parts.aspx?product_type=USED&amp;keyword={encoded}">中古商品</a></li>
    </ul><div class="list-interface-bar"></div><section class="list_settings">
    <form action="/search_result.aspx" method="get" name="search">
    <input type="hidden" name="keyword" value="{escape(query, quote=True)}">
    <input type="hidden" name="product_type" value="ALL">
    </form></section><section class="paging_settings">
    <p class="pg_number_set"><span>1</span>件 (全7点)</p></section>
    <ul id="change_style_list" class="product_list">{contents}</ul></section>
    <script>{script}</script></body></html>'''

def page(query=JAN, *, body=None, url=None, status=200):
    return Page(url or HOST + '/search_result.aspx?keyword=' + quote(query, safe=''),
                (body if body is not None else shell(query)).encode('utf-8'), OBSERVED,
                content_type='text/html; charset=utf-8', status=status)
