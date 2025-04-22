from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import os
import tempfile
import uuid
import time
import shutil
import asyncio
import sys
import io
from typing import Any, Dict, List, Optional, Union
import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from mcp.server import Server
import uvicorn
import argparse
import tarfile
import io as BytesIO
import logging
import json
import threading

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("code-sandbox-mcp")

# 加载环境变量
load_dotenv()

# 创建MCP实例
mcp = FastMCP("code-sandbox-mcp")

# 获取一个Docker客户端
docker_client = None
docker_available = False

# 默认的Docker镜像
DEFAULT_IMAGE = "python:3.9-slim"

# 常驻容器的名称和ID
PERSISTENT_CONTAINER_NAME = "code_sandbox_persistent"
persistent_container_id = None
container_lock = threading.Lock()  # 用于控制容器访问的锁

# 检查Docker是否可用
try:
    docker_client = docker.from_env()
    docker_client.ping()  # 测试Docker守护进程连接
    docker_available = True
    logger.info("Docker服务已连接")
except Exception as e:
    logger.error(f"Docker服务不可用: {e}")
    logger.error("无法使用Docker容器沙箱功能")

def ensure_persistent_container():
    """确保持久化容器正在运行"""
    global persistent_container_id
    
    with container_lock:
        if persistent_container_id is not None:
            # 检查容器是否仍在运行
            try:
                container = docker_client.containers.get(persistent_container_id)
                if container.status == "running":
                    return persistent_container_id
            except NotFound:
                persistent_container_id = None
                logger.warning("持久化容器已被删除，将重新创建")
        
        # 尝试查找已有的容器
        try:
            existing = docker_client.containers.get(PERSISTENT_CONTAINER_NAME)
            if existing.status != "running":
                existing.start()
            persistent_container_id = existing.id
            logger.info(f"使用已存在的持久化容器，ID: {persistent_container_id}")
            return persistent_container_id
        except NotFound:
            pass  # 容器不存在，继续创建新容器
        
        # 创建新的持久化容器
        try:
            logger.info(f"拉取Docker镜像: {DEFAULT_IMAGE}")
            docker_client.images.pull(DEFAULT_IMAGE)
            
            # 创建容器配置
            container = docker_client.containers.run(
                image=DEFAULT_IMAGE,
                name=PERSISTENT_CONTAINER_NAME,
                working_dir="/app",
                detach=True,
                tty=True,
                stdin_open=True,
                remove=False,  # 不自动删除
                ports={},  # 不映射端口
                network_mode="none",  # 禁用网络
                cap_drop=["ALL"],  # 移除所有权限
                security_opt=["no-new-privileges"],  # 安全选项
                mem_limit="256m",  # 内存限制
                cpu_quota=100000,  # CPU限制
                cpu_period=100000,  # CPU周期
                runtime="nvidia",  # 使用NVIDIA运行时
                device_requests=[
                    docker.types.DeviceRequest(
                        count=-1,  # 使用所有可用GPU
                        capabilities=[['gpu']]
                    )
                ]
            )
            
            persistent_container_id = container.id
            logger.info(f"创建持久化容器成功, ID: {persistent_container_id}")
            
            # 初始化容器
            container.exec_run(["mkdir", "-p", "/app/workspaces"])
            
            return persistent_container_id
        except Exception as e:
            logger.error(f"创建持久化容器失败: {e}")
            raise
@mcp.tool()
async def sandbox_initialize(image: str = DEFAULT_IMAGE, use_persistent: bool = True) -> Dict[str, Any]:
    """
    初始化一个新的代码执行计算环境。
    基于指定的Docker镜像创建一个容器，默认使用Python slim镜像。
    
    参数:
    image: 作为基础环境的Docker镜像 (例如 'python:3.9-slim')
    use_persistent: 是否使用持久化容器（如果为True，image参数将被忽略）
    
    返回:
    包含容器ID和工作区ID的字典，可用于与该环境交互
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        if use_persistent:
            # 检查持久化容器是否已存在
            try:
                existing = docker_client.containers.get(PERSISTENT_CONTAINER_NAME)
                container_id = existing.id
                workspace_id = str(uuid.uuid4())
                workspace_path = f"/app/workspaces/{workspace_id}"
                
                # 在容器中创建工作区目录
                existing.exec_run(["mkdir", "-p", workspace_path])
                
                return {
                    "success": True,
                    "container_id": container_id,
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                    "mode": "persistent",
                    "message": "使用已存在的持久化容器"
                }
            except NotFound:
                # 持久化容器不存在，创建新容器
                container_id = ensure_persistent_container()
                workspace_id = str(uuid.uuid4())
                workspace_path = f"/app/workspaces/{workspace_id}"
                
                # 在容器中创建工作区目录
                container = docker_client.containers.get(container_id)
                container.exec_run(["mkdir", "-p", workspace_path])
                
                return {
                    "success": True,
                    "container_id": container_id,
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                    "mode": "persistent"
                }
        else:
            # 使用传统的独立容器模式
            logger.info(f"拉取Docker镜像: {image}")
            docker_client.images.pull(image)
            
            # 创建容器配置
            container = docker_client.containers.run(
                image=image,
                working_dir="/app",
                detach=True,
                tty=True,
                stdin_open=True,
                remove=False,  # 不自动删除
                ports={},  # 不映射端口
                network_mode="none",  # 禁用网络
                cap_drop=["ALL"],  # 移除所有权限
                security_opt=["no-new-privileges"],  # 安全选项
                mem_limit="256m",  # 内存限制
                cpu_quota=100000,  # CPU限制
                cpu_period=100000,  # CPU周期
            )
            
            container_id = container.id
            logger.info(f"创建独立容器成功, ID: {container_id}")
            
            return {
                "success": True,
                "container_id": container_id,
                "mode": "standalone"
            }
    except Exception as e:
        logger.error(f"创建容器失败: {e}")
        return {
            "success": False,
            "error": f"创建容器失败: {str(e)}"
        }
@mcp.tool()
async def sandbox_exec(container_id: str, commands: List[str], workspace_id: str = None) -> Dict[str, Any]:
    """
    在沙箱环境中执行命令。
    在指定的容器中运行一个或多个shell命令并返回输出。
    
    参数:
    container_id: 从initialize调用返回的容器ID
    commands: 要在沙箱环境中运行的命令列表
    workspace_id: 工作区ID（用于持久化容器模式）
    
    返回:
    包含每个命令执行结果的字典
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        results = []
        
        # 判断是否是持久化容器模式
        is_persistent = workspace_id is not None
        working_dir = f"/app/workspaces/{workspace_id}" if is_persistent else "/app"
        
        # 首先检查并安装所需的Python包
        try:
            # 检查pip是否可用
            _, pip_output = container.exec_run(
                cmd=["which", "pip"],
                stdout=True,
                stderr=True
            )
            
            if not pip_output:
                # 如果pip不可用，先安装pip
                logger.info(f"在容器 {container_id} 中安装pip")
                container.exec_run(["apt-get", "update"])
                container.exec_run(["apt-get", "install", "-y", "python3-pip"])
        except Exception as e:
            logger.warning(f"安装pip失败: {e}")
            
        # 执行每个命令
        for cmd in commands:
            cmd_to_run = cmd
            if is_persistent:
                # 在持久化模式下，确保命令在正确的工作目录中执行
                cmd_to_run = f"cd {working_dir} && {cmd}"
                
            logger.info(f"在容器 {container_id} 中执行命令: {cmd_to_run}")
            
            # 如果命令是python脚本，先尝试安装所需的包
            if cmd_to_run.startswith("python"):
                try:
                    # 执行pip install命令安装可能缺失的包
                    container.exec_run(["pip", "install", "--upgrade", "pip"])
                    container.exec_run(["pip", "install", "-r", "requirements.txt"])
                except Exception as e:
                    logger.warning(f"安装Python包失败: {e}")
            
            # 使用exec_run执行命令
            exit_code, output = container.exec_run(
                cmd=["sh", "-c", cmd_to_run],
                stdout=True,
                stderr=True,
                demux=True,  # 将stdout和stderr分开
            )
            
            # 处理输出
            stdout, stderr = output
            stdout = stdout.decode('utf-8') if stdout else ""
            stderr = stderr.decode('utf-8') if stderr else ""
            
            results.append({
                "command": cmd,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "success": exit_code == 0
            })
            
            # 如果命令失败，停止执行后续命令
            if exit_code != 0:
                logger.warning(f"命令 '{cmd}' 执行失败，退出代码: {exit_code}")
                break
                
        return {
            "success": True,
            "results": results
        }
    except Exception as e:
        logger.error(f"执行命令失败: {e}")
        return {
            "success": False,
            "error": f"执行命令失败: {str(e)}"
        }

@mcp.tool()
async def write_file_sandbox(container_id: str, file_name: str, file_contents: str, workspace_id: str = None, dest_dir: str = None) -> Dict[str, Any]:
    """
    将文件写入沙箱文件系统。
    在容器中创建具有指定内容的文件。
    
    参数:
    container_id: 从initialize调用返回的容器ID
    file_name: 要创建的文件名
    file_contents: 写入文件的内容
    workspace_id: 工作区ID（用于持久化容器模式）
    dest_dir: 创建文件的目录，相对于工作目录
    
    返回:
    写入操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        
        # 判断是否是持久化容器模式
        is_persistent = workspace_id is not None
        base_dir = f"/app/workspaces/{workspace_id}" if is_persistent else "/app"
        
        # 确定目标目录
        if dest_dir is None:
            target_dir = base_dir
        else:
            # 确保dest_dir不是绝对路径（安全措施）
            if dest_dir.startswith('/'):
                dest_dir = dest_dir.lstrip('/')
            target_dir = os.path.join(base_dir, dest_dir)
            
        # 确保目标目录存在
        mkdir_cmd = f"mkdir -p {target_dir}"
        container.exec_run(["sh", "-c", mkdir_cmd])
        
        # 计算完整文件路径
        if target_dir.endswith("/"):
            target_dir = target_dir[:-1]  # 移除结尾的斜杠
        
        file_path = f"{target_dir}/{file_name}"
        logger.info(f"写入文件到容器 {container_id}: {file_path}")
        
        # 创建包含文件内容的内存tarball
        tar_stream = BytesIO.BytesIO()
        tar = tarfile.open(fileobj=tar_stream, mode='w')
        
        # 添加文件到tarball
        file_data = file_contents.encode('utf-8')
        file_obj = BytesIO.BytesIO(file_data)
        
        tarinfo = tarfile.TarInfo(name=file_name)
        tarinfo.size = len(file_data)
        tarinfo.mtime = time.time()
        tar.addfile(tarinfo, file_obj)
        tar.close()
        
        # 将tarball的内容重置到开始位置
        tar_stream.seek(0)
        
        # 将tarball复制到容器
        container.put_archive(target_dir, tar_stream)
        
        return {
            "success": True,
            "file_path": file_path
        }
    except Exception as e:
        logger.error(f"写入文件失败: {e}")
        return {
            "success": False,
            "error": f"写入文件失败: {str(e)}"
        }

@mcp.tool()
async def copy_file(container_id: str, local_src_file: str, workspace_id: str = None, dest_path: str = None) -> Dict[str, Any]:
    """
    将单个文件复制到沙箱文件系统。
    将本地文件传输到指定的容器。
    
    参数:
    container_id: 从initialize调用返回的容器ID
    local_src_file: 本地文件系统中文件的路径
    workspace_id: 工作区ID（用于持久化容器模式）
    dest_path: 在沙盒环境中保存文件的路径，相对于工作目录
    
    返回:
    复制操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        
        # 检查源文件是否存在
        if not os.path.exists(local_src_file):
            return {
                "success": False,
                "error": f"本地文件不存在: {local_src_file}"
            }
        
        # 判断是否是持久化容器模式
        is_persistent = workspace_id is not None
        base_dir = f"/app/workspaces/{workspace_id}" if is_persistent else "/app"
            
        # 如果未指定目标路径，使用与源文件相同的名称
        if dest_path is None:
            dest_path = os.path.basename(local_src_file)
        
        # 确保dest_path不是绝对路径（安全措施）
        if dest_path.startswith('/'):
            dest_path = dest_path.lstrip('/')
        
        # 构造完整目标路径
        full_dest_path = f"{base_dir}/{dest_path}"
        
        # 确保目标目录存在
        dest_dir = os.path.dirname(full_dest_path)
        if dest_dir:
            mkdir_cmd = f"mkdir -p {dest_dir}"
            container.exec_run(["sh", "-c", mkdir_cmd])
        
        logger.info(f"复制文件 {local_src_file} 到容器 {container_id}: {full_dest_path}")
        
        # 创建包含文件的内存tarball
        tar_stream = BytesIO.BytesIO()
        tar = tarfile.open(fileobj=tar_stream, mode='w')
        
        # 添加文件到tarball
        tar.add(local_src_file, arcname=os.path.basename(full_dest_path))
        tar.close()
        
        # 将tarball的内容重置到开始位置
        tar_stream.seek(0)
        
        # 将tarball复制到容器
        container.put_archive(os.path.dirname(full_dest_path) or '/', tar_stream)
        
        return {
            "success": True,
            "file_path": full_dest_path
        }
    except Exception as e:
        logger.error(f"复制文件失败: {e}")
        return {
            "success": False,
            "error": f"复制文件失败: {str(e)}"
        }

@mcp.tool()
async def copy_project(container_id: str, local_src_dir: str, workspace_id: str = None, dest_dir: str = None) -> Dict[str, Any]:
    """
    将目录复制到沙箱文件系统。
    将本地目录及其内容传输到指定的容器。
    
    参数:
    container_id: 从initialize调用返回的容器ID
    local_src_dir: 本地文件系统中目录的路径
    workspace_id: 工作区ID（用于持久化容器模式）
    dest_dir: 在沙盒环境中保存源目录的路径，相对于容器工作目录
    
    返回:
    复制操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        
        # 检查源目录是否存在
        if not os.path.isdir(local_src_dir):
            return {
                "success": False,
                "error": f"本地目录不存在: {local_src_dir}"
            }
        
        # 判断是否是持久化容器模式
        is_persistent = workspace_id is not None
        base_dir = f"/app/workspaces/{workspace_id}" if is_persistent else "/app"
        
        # 确定目标目录
        if dest_dir is None:
            target_dir = base_dir
        else:
            # 确保dest_dir不是绝对路径（安全措施）
            if dest_dir.startswith('/'):
                dest_dir = dest_dir.lstrip('/')
            target_dir = os.path.join(base_dir, dest_dir)
        
        # 确保目标目录存在
        mkdir_cmd = f"mkdir -p {target_dir}"
        container.exec_run(["sh", "-c", mkdir_cmd])
        
        logger.info(f"复制目录 {local_src_dir} 到容器 {container_id}: {target_dir}")
        
        # 创建包含目录内容的内存tarball
        tar_stream = BytesIO.BytesIO()
        tar = tarfile.open(fileobj=tar_stream, mode='w')
        
        # 获取源目录的basename
        src_dir_name = os.path.basename(os.path.normpath(local_src_dir))
        
        # 添加源目录的内容到tarball
        for root, dirs, files in os.walk(local_src_dir):
            # 计算当前目录与源目录的相对路径
            arcroot = os.path.join(src_dir_name, os.path.relpath(root, local_src_dir))
            
            # 添加文件到tarball
            for file in files:
                file_path = os.path.join(root, file)
                arc_path = os.path.join(arcroot, file) if arcroot != src_dir_name else os.path.join("", file)
                tar.add(file_path, arcname=arc_path)
        
        tar.close()
        
        # 将tarball的内容重置到开始位置
        tar_stream.seek(0)
        
        # 将tarball复制到容器
        container.put_archive(target_dir, tar_stream)
        
        return {
            "success": True,
            "dest_dir": target_dir
        }
    except Exception as e:
        logger.error(f"复制目录失败: {e}")
        return {
            "success": False,
            "error": f"复制目录失败: {str(e)}"
        }

@mcp.tool()
async def copy_file_from_sandbox(container_id: str, container_src_path: str, workspace_id: str = None, local_dest_path: str = None) -> Dict[str, Any]:
    """
    将单个文件从沙箱文件系统复制到本地文件系统。
    将文件从指定容器传输到本地系统。
    
    参数:
    container_id: 要复制的容器ID
    container_src_path: 容器中要复制的文件路径
    workspace_id: 工作区ID（用于持久化容器模式）
    local_dest_path: 在本地文件系统中保存文件的路径
    
    返回:
    复制操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        
        # 判断是否是持久化容器模式
        is_persistent = workspace_id is not None
        
        # 如果是持久化模式且路径不是绝对路径，则添加工作区路径前缀
        if is_persistent and not container_src_path.startswith('/'):
            container_src_path = f"/app/workspaces/{workspace_id}/{container_src_path}"
        
        # 如果未指定本地目标路径，使用与源文件相同的名称
        if local_dest_path is None:
            local_dest_path = os.path.basename(container_src_path)
        
        logger.info(f"从容器 {container_id} 复制文件 {container_src_path} 到本地路径: {local_dest_path}")
        
        # 获取容器中的文件
        bits, stat = container.get_archive(container_src_path)
        
        # 创建包含目标目录的父目录（如果需要）
        parent_dir = os.path.dirname(local_dest_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        
        # 提取文件到本地文件系统
        file_obj = BytesIO.BytesIO()
        for chunk in bits:
            file_obj.write(chunk)
        file_obj.seek(0)
        
        # 解压tarball
        with tarfile.open(fileobj=file_obj) as tar:
            # 获取第一个文件成员（应该只有一个）
            member = tar.getmembers()[0]
            
            # 提取文件到目标路径
            with open(local_dest_path, 'wb') as f:
                f.write(tar.extractfile(member).read())
        
        return {
            "success": True,
            "local_path": local_dest_path,
            "file_size": os.path.getsize(local_dest_path)
        }
    except Exception as e:
        logger.error(f"从容器复制文件失败: {e}")
        return {
            "success": False,
            "error": f"从容器复制文件失败: {str(e)}"
        }

@mcp.tool()
async def sandbox_stop(container_id: str, is_persistent: bool = False) -> Dict[str, Any]:
    """
    停止并移除运行中的容器沙箱。
    如果是持久化容器，则仅清理而不停止它。
    
    参数:
    container_id: 要停止和删除的容器ID
    is_persistent: 是否是持久化容器
    
    返回:
    停止操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        # 如果是持久化容器，不实际停止它
        if is_persistent or container_id == persistent_container_id:
            logger.info(f"容器 {container_id} 是持久化容器，不会停止")
            return {
                "success": True,
                "message": f"容器 {container_id} 是持久化容器，已标记为保留"
            }
            
        # 否则，停止并移除容器
        container = docker_client.containers.get(container_id)
        
        logger.info(f"停止容器 {container_id}")
        container.stop(timeout=10)
        
        logger.info(f"移除容器 {container_id}")
        container.remove(v=True)  # v=True 同时移除关联的卷
        
        return {
            "success": True,
            "message": f"容器 {container_id} 已停止并移除"
        }
    except Exception as e:
        logger.error(f"停止容器失败: {e}")
        return {
            "success": False,
            "error": f"停止容器失败: {str(e)}"
        }

@mcp.tool()
async def clean_workspace(container_id: str, workspace_id: str) -> Dict[str, Any]:
    """
    清理持久化容器中的工作区。
    
    参数:
    container_id: 容器ID
    workspace_id: 要清理的工作区ID
    
    返回:
    清理操作的结果
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        workspace_path = f"/app/workspaces/{workspace_id}"
        
        logger.info(f"清理容器 {container_id} 中的工作区: {workspace_path}")
        
        # 删除工作区目录
        container.exec_run(["rm", "-rf", workspace_path])
        
        return {
            "success": True,
            "message": f"已清理工作区: {workspace_path}"
        }
    except Exception as e:
        logger.error(f"清理工作区失败: {e}")
        return {
            "success": False,
            "error": f"清理工作区失败: {str(e)}"
        }

@mcp.tool()
async def get_container_logs(container_id: str) -> Dict[str, Any]:
    """
    获取容器的日志输出。
    返回指定容器的所有日志。
    
    参数:
    container_id: 要获取日志的容器ID
    
    返回:
    包含日志内容的字典
    """
    if not docker_available:
        return {
            "success": False,
            "error": "Docker服务不可用"
        }
    
    try:
        container = docker_client.containers.get(container_id)
        
        logger.info(f"获取容器 {container_id} 的日志")
        logs = container.logs().decode('utf-8')
        
        return {
            "success": True,
            "logs": logs
        }
    except Exception as e:
        logger.error(f"获取容器日志失败: {e}")
        return {
            "success": False,
            "error": f"获取容器日志失败: {str(e)}"
        }

# SSE传输设置
def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """创建一个可以通过SSE提供给定mcp服务器的Starlette应用程序。"""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

# 应用启动时启动一个持久化容器
def start_persistent_container():
    if docker_available:
        try:
            logger.info("启动时初始化持久化容器...")
            ensure_persistent_container()
            logger.info(f"持久化容器已准备就绪: {persistent_container_id}")
        except Exception as e:
            logger.error(f"初始化持久化容器失败: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='运行Code Sandbox MCP服务器')
    parser.add_argument('--host', default='0.0.0.0', help='绑定的主机')
    parser.add_argument('--port', type=int, default=9520, help='监听的端口')
    parser.add_argument('--no-persistent', action='store_true', help='禁用持久化容器模式')
    args = parser.parse_args()

    # 获取MCP服务器实例
    mcp_server = mcp._mcp_server

    # 初始化持久化容器（如果未禁用）
    if not args.no_persistent:
        start_persistent_container()

    # 创建Starlette应用
    starlette_app = create_starlette_app(mcp_server, debug=True)

    # 启动服务器
    print(f"启动Code Sandbox MCP服务器，监听 {args.host}:{args.port}")
    uvicorn.run(starlette_app, host=args.host, port=args.port)