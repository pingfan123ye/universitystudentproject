"""
讯飞语音识别配置
申请地址: https://www.xfyun.cn/service/voicedictation
免费额度: 新用户有免费体验包

⚠️ 请勿在此文件中填写真实密钥！
请在 .env 文件中设置:
  IFT_APPID=你的APPID
  IFT_API_KEY=你的APIKey
  IFT_API_SECRET=你的APISecret
"""
# 从统一配置模块读取（环境变量 + .env 文件）
from config import IFT_APPID, IFT_API_KEY, IFT_API_SECRET
