"""health 领域包。

`KNOWLEDGE_SCOPE` 是上传/抽取把文档索引进 chroma 时用的集合名，也是角色
`knowledge_scopes` 里要授权的那个名字。放在这里是为了让**写索引**（上传）与
**删索引**（删除报告）两端不可能各自写成两个相近但不同的字符串 —— 一旦写错，
表现是"删了报告但向量还在"或"索引写进去检索不到"，且都很难查。
"""

KNOWLEDGE_SCOPE = "health_reports"
