"""
PostgREST 代理插件。
将未匹配路由的请求转发到 PostgREST 服务（远程或本地数据库）。
支持三种模式：
  - PROXY_LOCAL=True: 本地模式，直接查询数据库（绕过 PostgREST）
  - PROXY_CUSTOM=True: 自定义模式，不自动拦截请求
  - 默认: 远程模式，转发到 PROXY_SERVICE_URL
"""
import os
from typing import Any, TYPE_CHECKING

import requests
from flask import request

from ...api.response import Response
from ...api.request import proxy

__all__ = [
    "init_app",
    "proxy_request",
    "proxy_response",
    "is_send_proxy",
    "local_run",
]

if TYPE_CHECKING:
    from flask import Flask
    flask_app: Flask
    server_url: str
    proxy_local: bool
else:
    flask_app = None
    server_url = None
    proxy_local = None


def init_app(app: "Flask") -> None:
    """
    初始化 PostgREST 代理，根据配置选择代理模式并注册 before_request 钩子。
    
    Args:
        app: Flask 应用实例，需配置 PROXY_SERVICE_URL 或 PROXY_LOCAL。
    """
    global flask_app, server_url, proxy_local
    flask_app = app
    server_url = flask_app.config.get("PROXY_SERVICE_URL") or os.environ.get("PROXY_SERVICE_URL", "http://postgrest:3000")  # 代理服务地址
    proxy_local = flask_app.config.get("PROXY_LOCAL") or (os.environ.get("PROXY_LOCAL", "False").lower() == "true")  # 本地代理
    proxy_custom = flask_app.config.get("PROXY_CUSTOM") or (os.environ.get("PROXY_CUSTOM", "False").lower() == "true")  # 个性化代理

    # get proxy config
    if proxy_custom:
        return

    @app.before_request
    def app_proxy():
         # request.url_rule
        if not is_send_proxy():
            return
        elif proxy_local:
            # 代理为本地数据库查询
            other_param = {}
            if request.data:
                other_param["json"] = request.json
                
            data, headers = local_run(
                method=request.method,
                url=request.path,
                params=request.args,
                headers=request.headers,
                **other_param,
            )
            return Response(data=data, headers=headers).make_flask_response()
        
        else:
            # 代理为远程服务查询
            return proxy(server_url)

def proxy_request(
    method: str = "GET",
    url: str = "",
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    params: dict[str, str] | None = None,
    includ_schema_prefix: bool = True,
    custom_server_url: str | None = None,
    **kwargs: Any,
) -> Response:
    """
    发送请求到 PostgREST 服务（远程或本地）。
    
    Args:
        method: HTTP 方法，默认 GET。
        url: 请求路径，如 /table。
        headers: 请求头字典。
        params: URL 查询参数字典。
        includ_schema_prefix: 是否从 URL 提取 schema 前缀并设置 Profile 头。
        custom_server_url: 自定义服务地址，为空时使用全局配置。
        **kwargs: 其他 requests.request 参数（如 json, data）。
    
    Returns:
        Response: 标准响应封装对象。
    """
    global flask_app

    # 使用传入的 server_url 参数，如果没有则使用全局配置
    global server_url
    target_url = custom_server_url if custom_server_url else server_url

    # 处理 includ_schema_prefix 逻辑
    if includ_schema_prefix:
        # 从 URL 中提取 schema（第一个 path 段）
        raw_path = url.lstrip("/")
        if raw_path:
            parts = raw_path.split("/", 1)
            
            schema = parts[0] if parts[0] else None
            
            if schema and len(parts) > 1:
                remaining = "/" + parts[1] if len(parts) > 1 else "/"

                # 根据方法设置 Accept-Profile 或 Content-Profile
                if method.upper() in ("GET", "HEAD"):
                    if headers is None:
                        headers = {}
                    elif not isinstance(headers, dict):
                        headers = dict(headers)
                    headers["Accept-Profile"] = schema
                else:
                    if headers is None:
                        headers = {}
                    elif not isinstance(headers, dict):
                        headers = dict(headers)
                    headers["Content-Profile"] = schema
                    
                # 更新 URL，去掉 schema 前缀
                url = remaining
        else:
            url = "/"

    # 本地代理
    if proxy_local:
        data, headers = local_run(
            method=method, url=url, params=params, headers=headers, **kwargs
        )
        return Response(data=data, headers=headers)

    send_headers = {}
    if headers:
        send_headers = (
            {h[0]: h[1] for h in headers} if not isinstance(headers, dict) else dict(headers)
        )
        # hop-by-hop/body 声明头由 requests 自行管理，透传会导致无 body 的 DELETE 请求挂起（对端等待 body 直到超时）
        for key in ("Content-Length", "Transfer-Encoding", "Connection", "Keep-Alive"):
            send_headers.pop(key, None)
            send_headers.pop(key.lower(), None)
        send_headers["Authorization"] = None
        send_headers["Host"] = None

    response = requests.request(
        method=method,
        url=target_url + url,
        params=params,
        headers=send_headers,
        **kwargs,
    )

    if (
        "content-type" in response.headers
        and "json" in response.headers["content-type"]
        and response.text
    ):
        response_json = response.json()
    else:
        response_json = {}
        
    for key in ["Content-Encoding", "Content-Length"]:
        if key in response.headers:
            response.headers.pop(key)
        
    return Response(
        result=response.ok,
        code=response_json.get("code") if not response.ok else 0,
        message=response_json.get("message") if not response.ok else None,
        detail=response_json.get("details") if not response.ok else None,
        data=response_json if response.ok else None,
        http_status=response.status_code if response.status_code != 204 else 200,
        headers=response.headers,
    )

def proxy_response(response: requests.Response) -> tuple[bytes, int, dict[str, str]]:
    """
    将 requests.Response 转换为 (content, status_code, headers) 元组，过滤传输编码头。
    
    Args:
        response: requests.Response 对象。
    
    Returns:
        tuple: (响应内容, 状态码, 过滤后的头部字典)。
    """

    headers = {
        key: response.headers.get(key)
        for key in response.headers
        if key not in ["Transfer-Encoding", "Content-Encoding", "Content-Location"]
    }

    return response.content, response.status_code, headers


def is_send_proxy() -> bool:
    """
    判断当前请求是否应转发到代理服务。
    规则：有 url_rule 且未设置 proxy 头的请求不转发（表示已匹配到应用路由）。
    
    Returns:
        bool: 需要转发返回 True，否则返回 False。
    """
    global flask_app, server_url

    # request.url_rule
    if not flask_app or (request.url_rule and not request.headers.get("proxy")):
        return False

    return True


def local_run(
    schema: str | None = None,
    method: str = "GET",
    url: str = "",
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    data: list[Any] = [],
    **kwargs: Any,
) -> tuple[Any, dict[str, str]]:
    """
    本地模式：直接将 PostgREST 风格的查询转换为 SQL 并执行。
    支持 SELECT（含分页、计数）、INSERT/UPDATE/DELETE（含 RETURNING）。
    
    Args:
        schema: 数据库 schema，为空时使用 PRODUCT_KEY。
        method: HTTP 方法。
        url: 请求路径，如 /table。
        headers: 请求头（用于判断 Accept 类型）。
        params: URL 查询参数（PostgREST 风格）。
        data: 请求体（JSON 数据）。
        **kwargs: 其他参数（未使用）。
    
    Returns:
        tuple: (数据列表或字典, 响应头字典)，响应头可能包含 Content-Range。
    """
    global flask_app

    from .sql_generator import generate_sql
    from ...extension.database import db

    # 调用本地转换
    exec_sql, count_sql = generate_sql(
        method or request.method,
        url or request.path,
        params or request.args,
        data or request.get_json(silent=True),
        headers or request.headers,
    )

    schema = schema or flask_app.config.get("PRODUCT_KEY")
    first_sql = f"set search_path to {schema};"

    # 执行语句
    data = None
    headers = {}
    if isinstance(exec_sql, list):
        for item_sql in exec_sql:
            if "returning" in item_sql:
                item = db.session.execute(first_sql + item_sql).fetchone()
                data = data or []
                item and data.append(dict(item))
            else:
                db.session.execute(first_sql + item_sql)
                
    elif "select" in exec_sql:
        if "application/vnd.pgrst.object+json" in request.headers.get("Accept"):
            query_result = db.session.execute(first_sql + exec_sql).fetchone()
            data = dict(query_result)
        else:
            query_result = db.session.execute(first_sql + exec_sql).fetchall()
            count_result = db.session.execute(first_sql + count_sql).fetchone()

            if query_result is not None:
                data = [dict(row) for row in query_result]
            else:
                data = []

            if count_result is not None:
                count_result = count_result[0]
                index_begin = int(request.args.get("offset", "0"))
                index_end = (
                    min(
                        index_begin + int(request.args.get("limit", "99999999")),
                        count_result,
                    )
                    - 1
                )
                headers[
                    "Content-Range"
                ] = f"{str(index_begin)}-{str(index_end)}/{count_result}"
    else:
        
        if "returning" in exec_sql:
            query_result = db.session.execute(first_sql + exec_sql).fetchall()
            data = [dict(row) for row in query_result]
        else:
            db.session.execute(first_sql + exec_sql)
    
    return data, headers

