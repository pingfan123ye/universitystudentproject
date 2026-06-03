---
name: create-ppt-and-html
description: 为智能音箱项目生成发布会风格 PPT（python-pptx）和产品展示 HTML
---
# create_ppt&html — 项目展示文件制作

根据项目内容生成 PPT 和 HTML 展示文件。有以下已有模板可用：

## PPT 生成（python-pptx）
- 位置: `backend/../presentation/make_ppt.py`
- 已有模板: `presentation/make_ppt.py`, `presentation/make_slide1.py`, `presentation/make_slide1_v2.py`
- 图片素材: `presentation/images/` 目录下
- 运行: `python presentation/make_ppt.py`
- 输出: `presentation/MiGPT_发布会_v2.pptx`

## HTML 展示页
- 已有文件: `presentation/showcase.html`（34.7MB，含大量内嵌图片）
- 通常需要嵌入截图，体积较大
- 可以用纯文字+外链图片的形式重写轻量版

## 何时使用
用户提出"做个展示页"、"生成PPT"、"做个宣传页面"时，直接参考 `presentation/` 目录下的已有模板进行修改或扩展。优先复用 `make_ppt.py` 的 python-pptx 模板。
