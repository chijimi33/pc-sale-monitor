from unittest import TestCase

from sale_monitor.satellite.refusal import refusal_feature

NAV = "<nav>" + "".join(f'<a href="/c{i}">カテゴリ{i}</a>' for i in range(600)) + "</nav>"
# The page that stopped the first production run: a router whose specification
# list names a feature "Login Captcha". It is an ordinary product page.
ROUTER = ("<html><head><title>ASUS エイスース ROG Rapture GT-BE98 [ゲーミングルーター]｜ツクモ公式通販サイト</title>"
          '<!-- recaptcha enterprise telemetry --><script src="https://www.google.com/recaptcha/enterprise.js"></script>'
          "<style>.grecaptcha-badge { visibility: hidden; }</style></head><body>" + NAV +
          "<h1>ROG Rapture GT-BE98</h1><p>価格 98,802円</p><table><tr><td>Feedback system</td></tr>"
          "<tr><td>New Device Connect Notification</td></tr><tr><td>Login Captcha</td></tr>"
          "<tr><td>Connection Diagnosis</td></tr></table></body></html>")


# Anonymised copy of the page ARK served to automated requests on 2026-11-24
# (nonce, ray, token and option values replaced; structure and text unchanged).
CLOUDFLARE = ('<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
              '<meta name="robots" content="noindex,nofollow"><style>body{margin:0}</style>'
              '<meta http-equiv="refresh" content="360"></head><body><div class="main-wrapper" role="main">'
              '<div class="main-content"><noscript><div class="h2"><span id="challenge-error-text">'
              'Enable JavaScript and cookies to continue</span></div></noscript></div></div>'
              '<script nonce="n">(function(){window._cf_chl_opt = {cType: "managed", cRay: "0"};'
              'var a = document.createElement("script");'
              'a.src = "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=0";'
              'document.getElementsByTagName("head")[0].appendChild(a);}());</script></body></html>')
# An ordinary page behind Cloudflare Bot Management: the detection script is
# injected, but there is no challenge.
CLOUDFLARE_ORDINARY = ("<html><head><title>商品｜ショップ</title></head><body>" + NAV +
                       "<h1>GeForce RTX 5070</h1><p>価格 98,000円</p>"
                       "<noscript><p>Please enable JavaScript to use all features of this site.</p></noscript>"
                       "<script>window.__CF$cv$params={r:'0',t:'1'};</script>"
                       '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body></html>')


class RefusalFeatureTests(TestCase):
    def check(self, cases):
        for name, body, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(refusal_feature(body), expected)

    def test_words_in_product_text_are_not_a_refusal(self):
        self.check((
            ("router specification", ROUTER, None),
            ("heading about a feature", "<h2>不正なアクセスを防ぐ</h2><p>AiProtection がアクセスを拒否します。</p>", None),
            ("quoted example", "<p>エラー表示の例：「アクセスが拒否されました」</p>", None),
            ("review sentence", "<p>このルーターは不正なアクセスを検知しましたという通知を出してくれるので安心です</p>", None),
            ("list item with more text", "<li>不正なアクセスをブロックしました、と表示</li>", None),
            ("description starting with the notice", "<p>不正なアクセスを検知しましたという通知を出してくれるので安心です</p>", None),
            ("description of a display", "<li>不正なアクセスを検知しましたと表示する機能です。</li>", None),
            ("english description", "<p>Please verify you are human is an example of the message shown by this product.</p>", None),
            ("blocked by a feature", "<p>Your request was blocked by the parental control feature of this router</p>", None),
            ("traffic described", "<p>Detected unusual traffic alerts are sent to the app</p>", None),
            ("security check described", "<p>Complete the security check to continue is displayed on the admin page</p>", None),
            ("captcha as a product word", "<title>CAPTCHA 対応ルーター</title><h1>Login Captcha</h1><p>captcha</p>", None),
            ("comments scripts styles", "<html><title>x</title><!-- access denied --><script>var s='Access Denied'</script>"
                                        "<style>.captcha{}</style><body>ok</body></html>", None),
        ))

    def test_whole_block_notices_are_a_refusal_whatever_else_the_page_holds(self):
        self.check((
            ("after a long navigation", "<html><title>ツクモ</title><body>" + NAV + "<p>アクセスが拒否されました</p></body></html>", "notice"),
            ("beside product information", "<html><body><h1>GeForce RTX 5070</h1><p>価格 98,000円</p>"
                                           "<div>ロボットではないことを<b>確認</b>してください。</div></body></html>", "notice"),
            ("title", "<title>Access   Denied</title>", "notice"),
            ("status lead-in", "<h1>403 Forbidden</h1><p>Error: Access denied.</p>", "notice"),
            ("character references", "<p>&#x30A2;&#x30AF;&#x30BB;&#x30B9;が拒否されました。</p>", "notice"),
            ("line breaks", "<body>Reference #18.abc<br>Access Denied<br>more</body>", "notice"),
            ("permission sentence", '<p>You don\'t have permission to access "http://x/" on this server.</p>', "notice"),
            ("traffic notice with continuation", "<div>Our systems have detected unusual traffic from your computer network. "
                                                 "Please try again later.</div>", "notice"),
            ("human check", "<span>Please verify you are human</span>", "notice"),
            ("human check with continuation", "<p>Verify you are human by completing the action below.</p>", "notice"),
            ("blocked from this site", "<p>You have been blocked from accessing this site.</p>", "notice"),
            ("traffic notice, short", "<p>We detected unusual traffic.</p>", "notice"),
            ("security check to continue", "<h2>Please complete the security check to continue</h2>", "notice"),
            ("browser check with site name", "<p>Checking your browser before accessing shop.example.jp.</p>", "notice"),
            ("status line alone", "<h1>403 Forbidden</h1><p>nginx</p>", "notice"),
            ("detected, alone", "<div>不正なアクセスが検出されました</div>", "notice"),
            ("temporary restriction", "<p>お客様のアクセスを一時的に制限しております</p>", "notice"),
            ("detected", "<p>不正なアクセスを検知しました。</p>", "notice"),
            ("router page plus notice", ROUTER.replace("</body>", "<p>アクセスが拒否されました</p></body>"), "notice"),
        ))

    def test_challenge_widgets_in_the_fetched_html(self):
        self.check((
            ("class", "<html><body>" + NAV + '<div class="g-recaptcha" data-sitekey="k"></div></body></html>', "challenge_widget"),
            ("response field", '<body><textarea name="h-captcha-response"></textarea></body>', "challenge_widget"),
            ("iframe", '<body><iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe></body>', "challenge_widget"),
            ("library only", '<head><script src="https://www.google.com/recaptcha/enterprise.js"></script></head><body>ok</body>', None),
        ))

    def test_cloudflare_challenge_page(self):
        self.check((
            ("served page", CLOUDFLARE, "notice"),
            ("configuration object only", "<html><body><script>window._cf_chl_opt = {};</script></body></html>",
             "challenge_widget"),
            ("request sentence only", "<html><body><noscript><div>Enable JavaScript and cookies to continue</div>"
                                      "</noscript></body></html>", "notice"),
            ("error slot only", '<body><span id="challenge-error-text"></span></body>', "challenge_widget"),
            ("bot management on an ordinary page", CLOUDFLARE_ORDINARY, None),
            ("sentence described in text", "<p>Enable JavaScript and cookies to continue is shown by some sites.</p>", None),
        ))

    def test_plain_text_and_robots_bodies(self):
        self.check((
            ("plain notice", "ACCESS DENIED", "notice"),
            ("plain notice among lines", "Request blocked.\nAccess Denied\nReference 1", "notice"),
            ("robots directives", "User-agent: *\nDisallow: /captcha/\n# access denied area\nCrawl-delay: 5\n", None),
            ("robots replaced by a notice", "Access Denied\n", "notice"),
            ("robots replaced by a page", "<html><title>Access Denied</title><body>blocked</body></html>", "notice"),
            ("empty", "", None),
        ))
