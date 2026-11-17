from hashlib import sha256  # 用于生成唯一的hash_id
from pathlib import Path

from production_rag.domain.models import Document


def parse_document(
    file_path: str | Path,  # 文档名称（例如：demo.txt）
    root_dir: str
    | Path,  # 文档的根目录--用Path进行拼接，能够方便进行管理（路径例如:rag/data）与名字拼接才是完整的路径
    version: int,  # 文档的版本
    max_bytes: int = 5 * 1024 * 1024,  # 文档的最大大小:5MB
) -> Document:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes 必须是正整数")

    # 1. 确定知识库根目录。
    root = (
        Path(root_dir).expanduser().resolve(strict=True)
    )  # expanduser是替换当前目录前的~展开为根,resolve是+strict将其转化为真实的绝对路径

    if not root.is_dir():  # isdir用于判断目录是不是应该文件夹,返回Boolean
        raise NotADirectoryError(f"知识库根路径不是目录：{root}")
    """
    root.exists()判断路径是否存在
    root.is_dir()判断是否是文件夹
    root.is_file()判断是否是普通的文件
    """
    # 2. 将输入路径转换成真实文件路径。
    path = Path(file_path).expanduser()

    if not path.is_absolute():
        path = root / path
    # is_absolute()判断是绝对路径还是相对路径True是绝对/False是相对
    path = path.resolve(strict=True)
    """
    判断路径：如果是绝对的就直接下一步，如果是相对的就把他变成绝对的
    """
    if not path.is_relative_to(root):
        raise ValueError("文件必须位于指定的知识库根目录内")
    # is_relative_to:判断一个路径是否位于另外一个路径下面
    if not path.is_file():
        raise IsADirectoryError(f"输入路径不是普通文件：{path}")

    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("当前只支持 .md 和 .txt 文件")
    # path.suffix:用于获取文件后缀
    # 3. 有限量地读取文件，避免一次读入过大内容。
    with path.open("rb") as file:
        raw = file.read(max_bytes + 1)
    """
    read限制读取文件的大小,加上这个1就能知道到底有没有超过限制
    文件实际大小	最多读取 6 字节后得到的长度	判断
        4 字节	    4	    允许
        5 字节	    5	    允许
        6 字节	    6	    超限
        1000 字节	6	    超限
    """
    if len(raw) > max_bytes:
        raise ValueError(f"文件超过大小限制：{max_bytes} 字节")

    # 4. 解码并统一换行符，保留正文中的空白。
    text = raw.decode("utf-8-sig")  # 去掉潜藏的bom编码
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # 统一所有的换行

    if not text.strip():
        raise ValueError(f"文件正文为空：{path.name}")

    # 5. 默认使用文件名作为标题。
    # Markdown 首个非空行若为一级标题，则使用该标题。
    title = path.stem  # 取出不含后缀的文件名

    if path.suffix.lower() == ".md":  # 取出后缀
        for line in text.splitlines():
            if not line.strip():
                continue

            if line.startswith("# ") and line[2:].strip():
                title = line[2:].strip()
            break

    # 6. 使用知识库内的相对路径标识文档来源。
    source = path.relative_to(root).as_posix()  # as_posix统一斜杠为/
    document_id = sha256(source.encode("utf-8")).hexdigest()

    return Document(
        document_id=document_id,
        source=source,
        version=version,
        title=title,
        text=text,
    )
