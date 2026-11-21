# 实验室推荐 Agent

面向低年级学生的实验室推荐助手。学生用一句自然语言说方向、年级、在意什么（科研 / 比赛 / 就业），
系统从 45 个实验室里挑出最匹配的 3 个，给出理由、研究方向、招新情况，并能追问、对比、看完整档案；
老师端可以校对档案，改动会留快照、可回滚。学生画像跨会话记忆。

纯 Python 标准库实现（`http.server` + SSE），没有第三方依赖，不需要 `pip install`。

## 跑起来

```bash
# 1. 需要 Python 3.9+（开发与测试用的是 3.13）
python --version

# 2. 配密钥
cp .env.example .env      # Windows: copy .env.example .env
#    打开 .env，填 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY

# 3. 启动（端口可选，默认 8000）
python server/app.py
#    服务已启动： http://127.0.0.1:8000
```

浏览器打开 <http://127.0.0.1:8000> 就是首页。

| 页面 | 地址 | 说明 |
|---|---|---|
| 首页 | `/` | 今日被咨询最多的实验室气泡，点开进档案 |
| 学生端 | `/chat` | 主对话界面 |
| 教师端 | `/teacher` | 档案校对、解析、快照回滚 |
| 档案页 | `/lab?id=L01` | 单个实验室的完整信息 |

## 两个密钥分别管什么

| 变量 | 必需 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 是 | 理解学生描述、抽条件、生成推荐理由 |
| `DASHSCOPE_API_KEY` | 是 | `text-embedding-v4` 把问题转成 1024 维向量，做检索 |
| `LANGSMITH_*` | 否 | 调用链追踪；不填只走本地日志 |

索引已经随仓库提供，不需要重新嵌入文档；但**每次提问**都要用 DashScope 把问题转向量，所以这个 key 省不掉。

没配密钥时：页面能打开、实验室数据和首页气泡都正常，但一发消息会回一条
`出错了：LLMError: 未配置 DEEPSEEK_API_KEY（检查项目根目录 .env）`。

## 数据都在仓库里

| 路径 | 内容 |
|---|---|
| `data/labs/labs.json` | 45 个实验室的结构化档案（方向、招新、老师、地点、成果） |
| `data/parsed/`、`data/chunks/` | 从 `实验室介绍.pdf` 解析出的正文与分块 |
| `data/indexes/labs_index.sqlite3` | 向量索引 + BM25 字段，**已入库，开箱可用** |
| `data/eval/`、`data/review/` | 评测集、人工复核表 |

想从 PDF 重建（会消耗两个 key 的额度）：

```bash
python scripts/build_labs.py      # 结构化抽取
python scripts/build_chunks.py    # 分块
python scripts/build_vectors.py   # 嵌入 + 建索引
```

## 测试

```bash
python server/app.py &        # 用例会打 http://127.0.0.1:8000（脚本也会自动起一个）
python run_tests.py           # 12 套全跑，约 12–16 分钟
python run_tests.py --fast    # 跳过 UI 与教师链路，约 2 分钟
```

注意事项：

- 用例里会真的调大模型，需要 `.env` 里的两个 key。
- UI 类用例（`tests/ui_test.py`、`lab_test.py`、`nav_test.py`、`teacher_ui_test.py`）用无头 Edge 驱动，
  里面写死了 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；换机器或非 Windows 时改这一行即可。

## 目录

```
server/app.py          单文件 HTTP 服务：路由、SSE 流式、静态文件
server/static/         home / index(学生端) / lab / teacher 四个页面
scripts/               解析、分块、向量、推荐管线、打分、理由、教师校对、记忆
data/                  实验室数据、解析结果、索引、评测集
tests/                 回归用例（12 套）+ 无头浏览器驱动
run_tests.py           一键回归
实验室推荐Agent-PRD.md  产品与实现的完整推导（含每一版的取舍与踩坑）
```

## 没有入库的东西

`.env`（密钥）、`data/students/`（学生画像，属于个人数据）、`data/teacher/`（校对快照，每次一份全量副本）、
`shots/`（测试截图）、`tests/edge-profile*`（无头浏览器缓存）。这些都会在运行时自动生成。
