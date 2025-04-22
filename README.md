# Docker Code Sandbox MCP

Docker Code Sandbox MCP是一个安全、高效的代码执行服务，允许在隔离的Docker容器中执行Python和其他代码，同时提供MCP (Model Control Protocol) API接口，便于与AI助手集成。

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Docker](https://img.shields.io/badge/docker-required-blue.svg)

## 主要特性

- **持久化容器模式**：复用单一Docker容器执行代码，大幅提高响应速度
- **工作区隔离**：为每个执行环境创建独立的工作区，确保安全隔离
- **NVIDIA GPU支持**：自动检测并使用可用的GPU加速计算
- **智能依赖管理**：自动检测并安装Python依赖
- **完整的文件操作**：支持文件上传、下载和项目目录管理
- **安全保障**：
  - 网络隔离（禁用容器网络）
  - 权限限制（移除所有危险权限）
  - 资源限制（CPU和内存限制）
  - 执行超时控制

## 安装要求

- Python 3.8+
- Docker引擎
- NVIDIA Container Toolkit（可选，用于GPU支持）

## 快速开始

### 安装

```bash
# 克隆仓库
git clone https://github.com/SomeHappiness/python-sandbox.git
cd docker-code-sandbox-mcp

# 安装依赖
pip install -r requirements.txt
```

### 服务配置

创建`requirements.txt`文件：

```
docker
mcp
fastmcp
uvicorn
starlette
python-dotenv
```

### 启动服务

```bash
# 启动服务（默认使用持久化容器模式）
python main.py

# 禁用持久化容器模式
python main.py --no-persistent

# 指定监听地址和端口
python main.py --host 0.0.0.0 --port 9520
```

## 使用示例

### Python代码执行

```python
# 初始化执行环境
response = await sandbox_initialize()
container_id = response["container_id"]
workspace_id = response["workspace_id"]

# 写入Python代码
code = """
import numpy as np
import matplotlib.pyplot as plt

# 生成随机数据
data = np.random.randn(1000)
print(f"数据均值: {data.mean():.4f}")
print(f"数据标准差: {data.std():.4f}")

# 保存直方图
plt.figure(figsize=(8, 6))
plt.hist(data, bins=30)
plt.title('正态分布直方图')
plt.savefig('histogram.png')
print("已保存直方图")
"""

await write_file_sandbox(
    container_id=container_id, 
    workspace_id=workspace_id, 
    file_name="analysis.py", 
    file_contents=code
)

# 创建依赖文件
await write_file_sandbox(
    container_id=container_id, 
    workspace_id=workspace_id, 
    file_name="requirements.txt", 
    file_contents="numpy\nmatplotlib\n"
)

# 执行代码
result = await sandbox_exec(
    container_id=container_id,
    workspace_id=workspace_id,
    commands=["python analysis.py"]
)

# 获取生成的图像
await copy_file_from_sandbox(
    container_id=container_id,
    workspace_id=workspace_id,
    container_src_path="histogram.png",
    local_dest_path="./output/histogram.png"
)

# 清理工作区
await clean_workspace(container_id, workspace_id)
```

### GPU加速示例

```python
# 初始化GPU执行环境
response = await sandbox_initialize()
container_id = response["container_id"]
workspace_id = response["workspace_id"]

# 创建TensorFlow测试脚本
gpu_test_code = """
import tensorflow as tf
print("TensorFlow版本:", tf.__version__)
print("可用GPU:", tf.config.list_physical_devices('GPU'))

# 简单GPU测试
with tf.device('/GPU:0'):
    a = tf.constant([[1.0, 2.0], [3.0, 4.0]])
    b = tf.constant([[5.0, 6.0], [7.0, 8.0]])
    c = tf.matmul(a, b)
    print("计算结果:", c)
"""

await write_file_sandbox(
    container_id=container_id, 
    workspace_id=workspace_id, 
    file_name="gpu_test.py", 
    file_contents=gpu_test_code
)

# 创建依赖文件
await write_file_sandbox(
    container_id=container_id, 
    workspace_id=workspace_id, 
    file_name="requirements.txt", 
    file_contents="tensorflow\n"
)

# 执行代码
result = await sandbox_exec(
    container_id=container_id,
    workspace_id=workspace_id,
    commands=["python gpu_test.py"]
)

# 输出结果
print(result["results"][0]["stdout"])
```

## API参考

### `sandbox_initialize(image="python:3.9-slim", use_persistent=True)`

初始化执行环境，创建新容器或使用持久化容器。

**参数:**
- `image`: Docker镜像名称（仅在非持久化模式下使用）
- `use_persistent`: 是否使用持久化容器

**返回:**
包含`container_id`和`workspace_id`的字典。

### `sandbox_exec(container_id, commands, workspace_id=None)`

执行一系列命令。

**参数:**
- `container_id`: 要执行命令的容器ID
- `commands`: 要执行的命令列表
- `workspace_id`: 工作区ID（用于持久化容器模式）

**返回:**
包含每个命令执行结果的字典。

### `write_file_sandbox(container_id, file_name, file_contents, workspace_id=None, dest_dir=None)`

将文件写入沙箱环境。

**参数:**
- `container_id`: 容器ID
- `file_name`: 目标文件名
- `file_contents`: 文件内容
- `workspace_id`: 工作区ID
- `dest_dir`: 目标目录（相对于工作区）

### `copy_project(container_id, local_src_dir, workspace_id=None, dest_dir=None)`

复制整个项目目录到沙箱环境。

### `copy_file_from_sandbox(container_id, container_src_path, workspace_id=None, local_dest_path=None)`

从沙箱环境复制文件到本地系统。

### `clean_workspace(container_id, workspace_id)`

清理特定工作区。

### `sandbox_stop(container_id, is_persistent=False)`

停止并移除容器（持久化容器不会被实际停止）。

## 安全注意事项

本项目采取多种安全措施确保代码执行安全：

1. **容器隔离**: 使用Docker提供的隔离特性
2. **网络禁用**: 默认禁用容器网络访问
3. **文件系统隔离**: 使用独立工作区，防止跨用户访问
4. **资源限制**: 限制内存、CPU使用
5. **权限限制**: 移除容器中的所有危险权限

⚠️ **警告**: 尽管采取了安全措施，但执行不受信任的代码始终存在风险。建议在非关键环境中使用，并添加额外的安全层。

## 许可证

本项目采用MIT许可证，详见[LICENSE](LICENSE)文件。

## 贡献

欢迎贡献！请随时提交问题、功能请求或拉取请求。

## 致谢

本项目受到[code-sandbox-mcp](https://github.com/Automata-Labs-team/code-sandbox-mcp) Go项目的启发，使用Python进行了完全重写。
