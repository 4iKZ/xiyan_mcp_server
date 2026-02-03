
import logging
from .server import main

logger = logging.getLogger("xiyan_mcp_server")

if __name__ == "__main__":
    logger.info("__main__.py: 调用 server.main() 以支持命令行参数")
    main()