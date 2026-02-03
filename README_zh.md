# XiYan MCP 服务器

<p align="center">
  <a href="https://github.com/XGenerationLab/XiYan-SQL"><img alt="MCP Playwright" src="https://raw.githubusercontent.com/XGenerationLab/XiYan-SQL/main/xiyanGBI.png" height="60"/></a>
</p>
<p align="center">
  <b>一种模型上下文协议（MCP）服务器，支持通过自然语言查询数据库</b><br/>
  <sub>由<a href="https://github.com/XGenerationLab/XiYan-SQL">XiYan-SQL</a>提供技术支持，该项目在开放基准上实现了文本到SQL的最好性能</sub>
</p>

<p align="center">
💻 <a href="https://github.com/XGenerationLab/xiyan_mcp_server" >XiYan-mcp-server</a> | 
🌐 <a href="https://github.com/XGenerationLab/XiYan-SQL" >XiYan-SQL</a> |
📖 <a href="https://arxiv.org/abs/2507.04701"> Arxiv</a> | 
🏆 <a href="https://github.com/XGenerationLab/XiYanSQL-QwenCoder" >XiYanSQL Model</a> |
📄 <a href="https://paperswithcode.com/paper/xiyan-sql-a-multi-generator-ensemble" >PapersWithCode</a>
🤗 <a href="https://huggingface.co/collections/XGenerationLab/xiyansql-models-67c9844307b49f87436808fc">HuggingFace</a> |
🤖 <a href="https://modelscope.cn/collections/XiYanSQL-Models-4483337b614241" >ModelScope</a> |
🌕 <a href="https://bailian.console.aliyun.com/xiyan">析言GBI</a>
<br />
<img src="https://badge.mcpx.dev/?type=server%20%27MCP%20Server%27" alt="MCP Server" />
<a href="https://arxiv.org/abs/2411.08599"><img src="imgs/Paper-Arxiv-orange.svg" ></a>
<a href="https://opensource.org/licenses/Apache-2.0">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache 2.0" />
</a>
<a href="https://pepy.tech/projects/xiyan-mcp-server"><img src="https://static.pepy.tech/badge/xiyan-mcp-server" alt="PyPI Downloads"></a>
  <a href="https://smithery.ai/server/@XGenerationLab/xiyan_mcp_server"><img alt="Smithery Installs" src="https://smithery.ai/badge/@XGenerationLab/xiyan_mcp_server" height="20"/></a>
<a href="https://github.com/XGenerationLab/xiyan_mcp_server" target="_blank">
    <img src="https://img.shields.io/github/stars/XGenerationLab/xiyan_mcp_server?style=social" alt="GitHub stars" />
</a>
<br />
<a href="https://github.com/XGenerationLab/xiyan_mcp_server">英文</a> | <a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/README_zh.md">中文</a> | <a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/README_ja.md">日本語</a><br />
<a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/imgs/dinggroup_out.png">钉钉群</a> | 
<a href="https://weibo.com/u/2540915670" target="_blank">关注我</a>
</p>

## 目录

- [特性](#特性)
- [预览](#预览)
  - [架构](#架构)
  - [最佳实践](#最佳实践)
  - [工具预览](#工具预览)
- [安装](#安装)
  - [从 pip 安装](#从-pip-安装)
  - [从 Smithery.ai 安装](#从-smitheryai-安装)
- [配置](#配置)
  - [LLM 配置](#llm-配置)
    - [通用 LLMs](#通用-llms)
    - [Text-to-SQL 最新模型](#text-to-sql-最新模型)
    - [本地模型](#本地模型)
  - [数据库配置](#数据库配置)
    - [MySQL](#mysql)
    - [PostgreSQL](#postgresql)
- [启动](#启动)
  - [Claude Desktop](#claude-desktop)
  - [Cline](#cline)
  - [Goose](#goose)
  - [Cursor](#cursor)
- [它不起作用](#它不起作用)
- [引用](#引用)

## 特性
- 🌐 通过 [XiYanSQL](https://github.com/XGenerationLab/XiYan-SQL) 使用自然语言获取数据
- 🤖 支持通用 LLMs（如 GPT, qwenmax），文本到 SQL 最新模型
- 💻 支持纯本地模式（高安全性！）
- 📝 支持 MySQL 和 PostgreSQL。
- 🖱️ 列出可用表作为资源
- 🔧 读取表内容

## 预览
### 架构
有两种方式可以将该服务器集成到您的项目中，如下图所示：
左侧是远程模式，这是默认模式。它需要 API 密钥来访问服务提供商的 xiyanSQL-qwencoder-32B 模型（请参阅[配置](#配置)）。
另一种模式是本地模式，更加安全，不需要 API 密钥。

![architecture.png](imgs/architecture.png)

### 最佳实践和报告

["使用 MCP + Modelscope API 推理构建本地数据助手，无需编写一行代码"](https://mp.weixin.qq.com/s/tzDelu0W4w6t9C0_yYRbHA)

["Modelscope 上的 Xiyan MCP"](https://modelscope.cn/headlines/article/1142)

### 在 MCPBench 上的评估
下图展示了 XiYan MCP 服务在 MCPBench 基准测试中的表现。XiYan MCP 服务器的性能优于 MySQL MCP 服务和 PostgreSQL MCP 服务，领先 2-22 个百分点。详细的实验结果可以在 [MCPBench](https://github.com/modelscope/MCPBench) 和报告 ["MCP 服务器评估报告"](https://arxiv.org/abs/2504.11094) 中找到。

![exp_mcpbench.png](imgs/exp_mcpbench.png)

### 工具预览
 - 工具 ``get_data`` 提供了一个自然语言接口，用于从数据库中检索数据。该服务器将输入的自然语言转换为 SQL，并调用数据库返回查询结果。

 - ``{dialect}://{table_name}`` 资源允许在指定特定的 table_name 时从数据库中获取部分样本数据以供模型参考。
- ``{dialect}://`` 资源将列出当前数据库的名称。

## 安装
### 从 pip 安装

要求 Python 3.11 或更高版本。
您可以通过 pip 安装服务器，它将安装最新版本：

```bash
pip install xiyan-mcp-server
```

安装后，您可以直接通过以下命令运行服务器：
```bash
python -m xiyan_mcp_server
```
但在您完成以下配置之前，它不会提供任何功能。
您将获得一个 yml 文件。然后您可以通过以下方式运行服务器：
```yaml
env YML=path/to/yml python -m xiyan_mcp_server
```

### 从 Smithery.ai 安装
请参见 [@XGenerationLab/xiyan_mcp_server](https://smithery.ai/server/@XGenerationLab/xiyan_mcp_server)

未进行全面测试。

## 配置

您需要一个 YAML 配置文件来配置服务器。
提供了一个默认配置文件 config_demo.yml，内容如下：

```yaml
model:
  name: "XGenerationLab/XiYanSQL-QwenCoder-32B-2412"
  key: ""
  url: "https://api-inference.modelscope.cn/v1/"

database:
  host: "localhost"
  port: 3306
  user: "root"
  password: ""
  database: ""
```

### LLM 配置
``Name`` 是要使用的模型名称，``key`` 是模型的 API 密钥，``url`` 是模型的 API 地址。我们支持以下模型。

| 版本 | 通用 LLMs (GPT, qwenmax) | Modelscope 最新模型 | Dashscope 最新模型 | 本地 LLMs |
|----------|------------------------------------|-----------------------------|----------------------------------|----------------|
| 描述     | 基础，易于使用                     | 性能最好，稳定，推荐       | 性能最好，供试用                | 速度慢，高安全性 |
| 名称     | 官方模型名称（例如 gpt-3.5-turbo, qwen-max） | XGenerationLab/XiYanSQL-QwenCoder-32B-2412 | xiyansql-qwencoder-32b          | xiyansql-qwencoder-3b |
| 密钥     | 服务提供商的 API 密钥（例如 OpenAI, 阿里云） | modelscope 的 API 密钥 | 通过电子邮件获取的 API 密钥 | ""               |
| URL      | 服务提供商的端点（例如 "https://api.openai.com/v1"） | https://api-inference.modelscope.cn/v1/ | https://xiyan-stream.biz.aliyun.com/service/api/xiyan-sql | http://localhost:5090 |

#### 通用 LLMs
如果您想使用通用 LLMs，如 gpt3.5，您可以直接像这样配置：
```yaml
model:
  name: "gpt-3.5-turbo"
  key: "YOUR KEY "
  url: "https://api.openai.com/v1"
database:
```

如果您想使用来自阿里巴巴的 Qwen，比如 Qwen-max，您可以使用以下配置：
```yaml
model:
  name: "qwen-max"
  key: "YOUR KEY "
  url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
database:
```
#### Text-to-SQL 最新模型
我们推荐 XiYanSQL-qwencoder-32B（https://github.com/XGenerationLab/XiYanSQL-QwenCoder），这是文本到 SQL 的最新模型，参见 [Bird benchmark](https://bird-bench.github.io/)。
您可以有两种方式使用该模型：
(1) [Modelscope](https://www.modelscope.cn/models/XGenerationLab/XiYanSQL-QwenCoder-32B-2412)， (2) 阿里云 DashScope。

##### (1) Modelscope 版本
您需要从 Modelscope 申请一个 API 推理的 ``key``，网址: https://www.modelscope.cn/docs/model-service/API-Inference/intro
然后您可以使用以下配置：
```yaml
model:
  name: "XGenerationLab/XiYanSQL-QwenCoder-32B-2412"
  key: ""
  url: "https://api-inference.modelscope.cn/v1/"
```

请阅读我们的 [模型描述](https://www.modelscope.cn/models/XGenerationLab/XiYanSQL-QwenCoder-32B-2412) 获取更多详细信息。

##### (2) Dashscope 版本

我们在阿里云 DashScope 上部署了模型，因此您需要设置以下环境变量：
请将您的电子邮件发送给我以获取 ``key``。 (godot.lzl@alibaba-inc.com)
在电子邮件中，请附上以下信息：
```yaml
name: "YOUR NAME",
email: "YOUR EMAIL",
organization: "your college or Company or Organization"
```
我们将根据您的电子邮件发送 ``key`` 给您。您可以在 yml 文件中填写该 ``key``。
该 ``key``将在 1 个月、200 次查询或其他法律限制后过期。

```yaml
model:
  name: "xiyansql-qwencoder-32b"
  key: "KEY"
  url: "https://xiyan-stream.biz.aliyun.com/service/api/xiyan-sql"
database:
```

注意：该模型服务仅供试用，如果您需要在生产中使用，请与我们联系。

或者，您也可以在自己的服务器上自行部署模型 [XiYanSQL-qwencoder-32B](https://github.com/XGenerationLab/XiYanSQL-QwenCoder)。

#### 本地模型
注意：本地模型速度较慢（在我的 MacBook 上每个查询约 12 秒）。
如果您需要稳定快速的服务，我们仍然推荐使用 Modelscope 版本。

要在本地模式下运行 xiyan_mcp_server，您需要： 
1）一台至少具有 16GB 内存的 PC/Mac
2）6GB 硬盘空间

步骤 1：安装额外的 Python 包
```bash
pip install flask modelscope torch==2.2.2 accelerate>=0.26.0 numpy=2.2.3
```

步骤 2：（可选）手动下载模型
我们推荐 [xiyansql-qwencoder-3b](https://www.modelscope.cn/models/XGenerationLab/XiYanSQL-QwenCoder-3B-2502/)。
您可以手动下载模型：
```bash
modelscope download --model XGenerationLab/XiYanSQL-QwenCoder-3B-2502
```
这将占用您 6GB 的磁盘空间。

步骤 3：下载脚本并运行服务器。文件 src/xiyan_mcp_server/local_xiyan_server.py

```bash
python local_xiyan_server.py
```
服务器将在 http://localhost:5090/ 上运行。

步骤 4：准备配置并运行 xiyan_mcp_server
config.yml 应如下所示：
```yml
model:
  name: "xiyansql-qwencoder-3b"
  key: "KEY"
  url: "http://127.0.0.1:5090"
```

到目前为止，本地模式准备就绪。

### 数据库配置
``host``、``port``、``user``、``password``、``database`` 是数据库的连接信息。

您可以使用本地或任何远程数据库。现在我们支持 MySQL 和 PostgreSQL（很快支持更多方言）。

#### MySQL

```yaml
database:
  host: "localhost"
  port: 3306
  user: "root"
  password: ""
  database: ""
```
#### PostgreSQL
步骤 1：安装 Python 包
```bash
pip install psycopg2
```
步骤 2：准备 config.yml 如下：
```yaml
database:
  dialect: "postgresql"
  host: "localhost"
  port: 5432
  user: ""
  password: ""
  database: ""
```

注意 ``dialect`` 应为 ``postgresql`` 以适用于 PostgreSQL。

## 启动
### Claude Desktop
在您的 Claude Desktop 配置文件中添加以下内容，参考 <a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/imgs/claude_desktop.jpg">Claude Desktop 配置示例</a>
```json
{
    "mcpServers": {
        "xiyan-mcp-server": {
            "command": "/xxx/python",
            "args": [
                "-m",
                "xiyan_mcp_server"
            ],
            "env": {
                "YML": "PATH/TO/YML"
            }
        }
    }
}
```
**注意此处的python命令需要完整的python可执行文件路径（`/xxx/python`），否则会找不到python解释器，可以通过`which python`来确定此路径。使用其他非claude应用也是如此。**
### Cline
准备配置，参考 [Claude Desktop](#claude-desktop)

### Goose
在配置中添加以下命令，参考 <a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/imgs/goose.jpg">Goose 配置示例</a>

```yaml
env YML=path/to/yml /xxx/python -m xiyan_mcp_server
```
### Cursor
使用与 [Goose](#goose) 相同的命令。

### Witsy
在命令中添加以下内容：
```yaml
/xxx/python -m xiyan_mcp_server
```
添加一个环境变量：键为 YML，值为您 yml 文件的路径。
参考 <a href="https://github.com/XGenerationLab/xiyan_mcp_server/blob/main/imgs/witsy.jpg">Witsy 配置示例</a>

## 联系我们

如果您对我们的研究或产品感兴趣，请随时联系我们。

#### 联系信息:

刘义富, zhencang.lyf@alibaba-inc.com

#### 加入我们的钉钉群

<a href="https://github.com/XGenerationLab/XiYan-SQL/blob/main/xiyansql_dingding.png">Ding Group钉钉群</a> 


## 其他相关链接

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/xgenerationlab-xiyan-mcp-server-badge.png)](https://mseep.ai/app/xgenerationlab-xiyan-mcp-server)


## 致谢与共创者

本项目基于 [XGenerationLab/xiyan_mcp_server](https://github.com/XGenerationLab/xiyan_mcp_server) 开发。感谢原项目的所有贡献者和共创者：

- [XGenerationLab](https://github.com/XGenerationLab)
- [ahmedmustahid](https://github.com/ahmedmustahid)
- [YifuLiuL](https://github.com/YifuLiuL)
- [eltociear](https://github.com/eltociear)
- [lwsinclair](https://github.com/lwsinclair)
- [Matvey-Kuk](https://github.com/Matvey-Kuk)
- [willyomg](https://github.com/willyomg)
- [ZhuangbilityY](https://github.com/ZhuangbilityY)

感谢他们为本项目提供的核心技术支持与卓越贡献。

## 引用
如果您觉得我们的工作对您有帮助，欢迎给我们一个引用。

```bibtex
@article{XiYanSQL,
      title={XiYan-SQL: A Novel Multi-Generator Framework For Text-to-SQL}, 
      author={Yifu Liu and Yin Zhu and Yingqi Gao and Zhiling Luo and Xiaoxia Li and Xiaorong Shi and Yuntao Hong and Jinyang Gao and Yu Li and Bolin Ding and Jingren Zhou},
      year={2025},
      eprint={2507.04701},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2507.04701}, 
}
```
```bibtex
@article{xiyansql_pre,
      title={A Preview of XiYan-SQL: A Multi-Generator Ensemble Framework for Text-to-SQL}, 
      author={Yingqi Gao and Yifu Liu and Xiaoxia Li and Xiaorong Shi and Yin Zhu and Yiming Wang and Shiqi Li and Wei Li and Yuntao Hong and Zhiling Luo and Jinyang Gao and Liyu Mou and Yu Li},
      year={2024},
      journal={arXiv preprint arXiv:2411.08599},
      url={https://arxiv.org/abs/2411.08599},
      primaryClass={cs.AI}
}
