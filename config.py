USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

SCREEN = {
    "colorDepth": 24,
    "pixelDepth": 24,
    "height": 1152,
    "width": 2048,
    "availHeight": 1152,
    "availWidth": 2048,
}

VIEWPORT = {"width": 1324, "height": 842}

TEALEAF_APP_KEY = "76938917d7504ff7a962174c021690bd"
HCAPTCHA_SITEKEY = "884d15d9-b649-4bbb-8d1c-2d6f0eed75eb"


# 1024proxy 动态代理池。格式：host:port:username:password
# 默认不启用；生产环境不要把代理账号密码写入代码仓库。
# 如需启用，请通过环境变量 PAYPAL_PROXY_POOL 或 PAYPAL_PROXY_URL 注入：
#   PAYPAL_PROXY_ENABLED=1
#   PAYPAL_PROXY_POOL='host:port:username:password,host2:port:username:password'
# 或在部署平台的 secret manager 中配置同名变量。
PROXY_ENABLED = False
PROXY_POOL: list[str] = []


# kookeey 动态代理 API。任务启动时按国家替换 {country}（BR/US/BA/ID）。
# 可用环境变量 PAYPAL_DYNAMIC_PROXY_API 覆盖整段 URL。
DYNAMIC_PROXY_API = (
    "https://www.kookeey.com/pickdynamicips?"
    "t=2&auth=pwd&format=4&n=1&p=http&gate=us&g={country}&r=3&type=txt"
    "&sign=874086cfbdb353e32d67a6dbebd498af&accessid=8239626&upf=1,5&dl=%0D%0A"
)

