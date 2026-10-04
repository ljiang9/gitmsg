# gitmsg

`git diff --staged` 进，conventional-commit 提交信息出。

写完代码 `git add` 之后，懒得想 commit message？`gitmsg` 帮你起草：

```bash
cd your-repo
git add .
python -m gitmsg            # 打印建议的提交信息
python -m gitmsg --apply    # 确认后直接 git commit
```

## 为什么做这个

市面上有不少 AI commit 工具，但要么要装 Node 全家桶，要么把你的 diff 发到不知名的服务器。
gitmsg 的思路很简单：

- **纯 Python 标准库**，零 pip 依赖，一个文件，拷走即用
- 走你自己配的 **OpenAI 兼容接口**（OpenAI / DeepSeek / 本地 Ollama 都行）
- 默认**只打印不提交**，`--apply` 也要先问你，diff 永远先过你的眼
- `--dry-run` 可以先看发给模型的 prompt 长什么样

## 安装

Python 3.10+，不需要安装任何依赖：

```bash
git clone https://github.com/ljiang9/gitmsg.git
cd gitmsg
# 方式一：直接用模块方式
python -m gitmsg --help
# 方式二：加到 PATH 当命令用
chmod +x gitmsg.py && ln -s $PWD/gitmsg.py ~/bin/gitmsg
```

设置 API key：

```bash
export OPENAI_API_KEY="sk-..."
# 可选：换兼容接口 / 模型
export GITMSG_BASE_URL="https://api.deepseek.com/v1"
export GITMSG_MODEL="deepseek-chat"
```

## 用法

```
usage: gitmsg [-h] [--all | --range RANGE] [--apply] [--yes]
              [--lang {zh,en}] [--dry-run] [--why] [--version]

用 LLM 从 git diff 起草 conventional-commit 提交信息（默认只打印，不提交）
```

| 参数 | 说明 |
|---|---|
| （无） | 读取 `git diff --staged` |
| `--all` | 用未 stage 的改动（`git diff`） |
| `--range HEAD~3` | 分析一个 commit 区间 |
| `--apply` | 用生成的消息执行 `git commit`（会先确认） |
| `--yes` | `--apply` 时跳过确认 |
| `--lang en` | 生成英文提交信息（默认中文） |
| `--dry-run` | 只展示 prompt 和 diff 统计，不联网 |
| `--why` | 额外生成一行 `CHANGELOG:` 建议条目 |

示例输出：

```
变更来源：已 stage 的改动 (git diff --staged)
diff 统计：2 个文件，+45 -12
--- 建议的提交信息 ---
feat(auth): 支持邮箱验证码登录

新增 /login/email-code 接口与前端表单；
验证码 5 分钟有效，失败 5 次锁定 15 分钟。

CHANGELOG 建议：登录页新增「邮箱验证码登录」入口，无需密码即可登录。
```

## 细节

- **智能截断**：diff 超过 6000 字符自动截断并注明；`package-lock.json`、`*.min.js`、构建产物等噪声文件的 patch 会被省略（文件名保留计数）。
- **diff 统计**：每次运行先打印文件数和 `+`/`-` 行数，再给提交信息。
- **安全**：API key 只从环境变量读，永远不会出现在输出或报错里。
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)，
  类型限定为 `feat / fix / docs / refactor / test / chore`。

## License

MIT
