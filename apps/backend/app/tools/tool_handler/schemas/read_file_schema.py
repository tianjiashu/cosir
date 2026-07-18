"""read_file 工具的参数模式（纯 JSON Schema 字典）。"""

READ_FILE_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "相对于项目根目录的文件路径。",
        },
        "limit": {
            "type": "integer",
            "description": "最多返回的行数；缺省返回全部。",
        },
    },
    "required": ["path"],
}
