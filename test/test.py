import requests
import json
import re
import pandas as pd
from pathlib import Path

base_url = "http://localhost:8000/mcp"

headers = {
  "Content-Type": "application/json",
  "Accept": "application/json, text/event-stream"
}

def parse_sse_response(text):
  """解析 SSE 响应，提取 JSON 数据"""
  for line in text.split('\n'):
    if line.startswith('data: '):
      try:
        return json.loads(line[6:])
      except:
        pass
  return None

def test_query(query: str, format_type: str = "markdown"):
  """测试查询并指定输出格式
   
  Args:
    query: 自然语言查询
    format_type: 输出格式 (markdown, json, csv)
  """
  print(f"\n{'='*60}")
  print(f"查询: {query}")
  print(f"格式: {format_type}")
  print('='*60)
   
  # Step 1: 初始化会话
  init_request = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "test-client", "version": "1.0"}
    },
    "id": 1
  }
   
  try:
    resp = requests.post(base_url, headers=headers, json=init_request, timeout=60)
    session_id = resp.headers.get("Mcp-Session-Id")
    # 确保 session_id 是字符串类型
    if session_id is not None and not isinstance(session_id, str):
      # 如果是 ServerSession 对象尝试获取其 ID 属性
      if hasattr(session_id, 'id'):
        session_id = session_id.id
      else:
        session_id = str(session_id)
     
    if not session_id:
      print("无法获取 Session ID")
      return
     
    print(f"Session ID: {session_id[:16]}...")
     
    # Step 2: 发送 initialized 通知
    initialized_notification = {
      "jsonrpc": "2.0",
      "method": "notifications/initialized"
    }
     
    headers_with_session = {
      **headers,
      "Mcp-Session-Id": session_id
    }
     
    requests.post(base_url, headers=headers_with_session, json=initialized_notification, timeout=10)
    print("Initialized notification sent")
     
    # Step 3: 调用 get_data 工具（带格式参数）
    call_request = {
      "jsonrpc": "2.0",
      "method": "tools/call",
      "params": {
        "name": "get_data",
        "arguments": {
          "query": query,
          "format": format_type # 新增：指定输出格式
        }
      },
      "id": 2
    }
     
    resp = requests.post(base_url, headers=headers_with_session, json=call_request, timeout=300)
    print(f"HTTP Status: {resp.status_code}")
     
    # 解析 SSE 响应
    result = parse_sse_response(resp.text)
    if result:
      if "result" in result:
        content = result["result"].get("content", [])
        for item in content:
          if item.get("type") == "text":
            result_text = item.get('text', '')
            print(f"\n查询结果 ({format_type}):\n{result_text}")

            # 保存结果到文件
            save_result_to_file(session_id, result_text, format_type)
      elif "error" in result:
        print(f"错误: {result['error']}")
    else:
      print(f"原始响应:\n{resp.text[:500]}")
       
  except Exception as e:
    print(f"异常: {e}")


def save_result_to_file(session_id: str, content: str, format_type: str, output_dir: str = "query_results"):
  """将查询结果保存到文件

  Args:
    session_id: 会话ID，用作文件名
    content: 查询结果内容
    format_type: 输出格式 (markdown, json, csv, parquet)
    output_dir: 输出目录
  """
  try:
    # 创建输出目录
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 根据格式确定文件扩展名
    extension_map = {
      "markdown": ".md",
      "json": ".json",
      "csv": ".csv",
      "parquet": ".parquet"
    }
    extension = extension_map.get(format_type, ".txt")

    # 生成文件名
    filename = f"{session_id}{extension}"
    file_path = output_path / filename

    # Parquet 格式需要特殊处理
    if format_type == "parquet":
      df = parse_content_to_dataframe(content, format_type)
      if df is not None:
        df.to_parquet(file_path, index=False)
        print(f"\n结果已保存到文件: {file_path}")
      else:
        print(f"\n解析失败，无法保存为 Parquet 格式")
      return

    # 保存文件 (markdown, json, csv)
    with open(file_path, "w", encoding="utf-8") as f:
      f.write(content)

    print(f"\n结果已保存到文件: {file_path}")

  except Exception as e:
    print(f"\n保存文件失败: {e}")


def parse_content_to_dataframe(content: str, source_format: str = "csv") -> pd.DataFrame:
  """将不同格式的内容解析为 DataFrame，并适配 HDFS 查询脚本的字段要求"""
  df = None
  try:
    # --- 第一步：执行原始格式解析 ---
    if source_format == "parquet":
      # 这里的逻辑是处理即将转为 parquet 的原始文本
      if "," in content and "\n" in content:
        df = parse_csv_to_dataframe(content)
      elif content.startswith("{") or content.startswith("["):
        df = parse_json_to_dataframe(content)
      elif "|" in content:
        df = parse_markdown_to_dataframe(content)
    elif source_format == "csv":
      df = parse_csv_to_dataframe(content)
    elif source_format == "json":
      df = parse_json_to_dataframe(content)
    elif source_format == "markdown":
      df = parse_markdown_to_dataframe(content)

    # --- 第二步：字段适配逻辑 (关键修改点) ---
    if df is not None:
      # 1. 统一时间戳和数值列名
      # 将 GreptimeDB 的默认列名映射为 HDFS 脚本识别的 'timestamp'
      rename_map = {
        "greptime_timestamp": "timestamp",
        "greptime_value": "value"
      }
      df = df.rename(columns=rename_map)

      # 2. 检查并补全 'metric_name' 列
      # 117 脚本的过滤功能 (--metric-name) 强依赖此列
      if "metric_name" not in df.columns:
        # 你可以根据查询内容动态赋值，这里默认设为 'default_metric'
        df["metric_name"] = "default_metric"
       
      # 3. 确保数据类型正确 (可选)
      # 确保 timestamp 列是字符串或适合排序的格式，以适配 HDFS 脚本的 orderBy
      if "timestamp" in df.columns:
        df["timestamp"] = df["timestamp"].astype(str)

    return df

  except Exception as e:
    print(f"解析内容失败: {e}")
    return None


def parse_csv_to_dataframe(content: str) -> pd.DataFrame:
  """解析 CSV 内容为 DataFrame"""
  from io import StringIO
  return pd.read_csv(StringIO(content))


def parse_json_to_dataframe(content: str) -> pd.DataFrame:
  """解析 JSON 内容为 DataFrame"""
  data = json.loads(content)
  if isinstance(data, dict) and "data" in data:
    return pd.DataFrame(data["data"])
  elif isinstance(data, list):
    return pd.DataFrame(data)
  else:
    return pd.DataFrame([data])


def parse_markdown_to_dataframe(content: str) -> pd.DataFrame:
  """解析 Markdown 表格内容为 DataFrame"""
  lines = content.strip().split('\n')
  table_lines = []

  for line in lines:
    if '|' in line and '---' not in line:
      table_lines.append(line)

  if len(table_lines) < 2:
    return None

  # 解析表头
  headers = [cell.strip() for cell in table_lines[0].split('|')[1:-1]]

  # 解析数据行
  rows = []
  for line in table_lines[1:]:
    cells = [cell.strip() for cell in line.split('|')[1:-1]]
    if len(cells) == len(headers):
      rows.append(cells)

  df = pd.DataFrame(rows, columns=headers)

  # 尝试转换数值列
  for col in df.columns:
    try:
      df[col] = pd.to_numeric(df[col])
    except (ValueError, TypeError):
      pass

  return df


if __name__ == "__main__":
  query = "查询sundb_metrics库的sys_cpu_usage表，HOST_IP为'172.19.19.111'的近期的8000条数据，只要greptime_timestamp和greptime_value列"
  #query = "查询cpu使用率"

  # 测试四种格式
  print("\n" + "="*60)
  print("测试多种输出格式")
  print("="*60)

  for fmt in ["markdown", "json", "csv", "parquet"]:
    test_query(query, format_type=fmt)    

