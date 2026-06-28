本项目基于
https://github.com/kaguraaya/bw-leyuan
的项目进行修改

主要将其适配2026BW和优化操作自由度具体操作和功能可见如下


# BW 乐园预约脚本

这是一个用于参考学习的 Bilibili World 2025 乐园活动资格预约脚本，主要功能包括：

- 校验 B 站登录 Cookie
- 查询 BW 乐园活动和商品预约信息
- 按配置的日期、并发和提前量执行预约请求
- 支持 dry-run、延迟测试和简单压测
- 支持根据测压结果推荐线程和并发数
- 支持 NTP 和B站服务器时间校准（建议运行时优先校准）

## 环境

- Python 3.8+
- 必需依赖：`requests`
- 可选依赖：`orjson`、`psutil`、`httpx`

安装依赖：

```bash
pip install requests orjson psutil httpx
```

## 使用

请先在手机端B站 BW乐园 功能中验证激活门票！！！

脚本不会在代码里保存 Cookie。运行前任选一种方式配置：

```bash
set BW_COOKIE=你的完整B站Cookie
python bws.py
```

或在脚本同目录创建 `cookie.txt`，写入完整 Cookie 后运行：

```bash
python bws.py
```

如需调整日期、线程数、提前量等参数，可修改 `bw乐园.py` 中的 `TICKET_DAYS` 和 `CFG`。

## 注意

本项目仅用于 Python 网络请求、接口分析和自动化流程学习。请勿公开自己的 Cookie，并遵守 Bilibili 相关活动规则与平台条款。
