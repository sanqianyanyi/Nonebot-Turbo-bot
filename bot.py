import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot（会自动读取 .env）
nonebot.init()

# 注册 OneBot V11 适配器
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载 plugins 目录下的插件
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()