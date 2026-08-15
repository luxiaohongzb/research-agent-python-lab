# 全文与混合检索最佳实践

## 目标链路

```text
PDF → GROBID → TEI → Passage + CitationEdge → PostgreSQL/pgvector
                                                    ↓
Query → metadata + keyword + vector + citation graph → RRF → rerank → EvidenceCard
```

这套实现把“解析、召回、融合、重排、证据抽取”分开。每层都可替换、可离线测试，某个远端元数据源失败只产生 warning，不会丢掉其他 lane 的结果。

## 关键实践

1. **结构优先切块**：先保留 TEI 章节路径，再对超长段落按句子切块；每个 Passage 保存页码、PDF 坐标、字符偏移、解析器版本与 SHA-256。引用定位不依赖模型临时生成。
2. **双路本地召回**：PostgreSQL 使用 `tsvector` + GIN 负责精确词项，pgvector HNSW 负责语义近邻。索引层返回各自原始分数，但不直接相加。
3. **RRF 融合**：按各 lane 的名次融合，避免 BM25、余弦和 API relevance 的尺度漂移。默认 `k=60`，应通过 Recall@K、nDCG 和 citation coverage 调参。
4. **召回后重排**：默认多样性重排限制每篇论文最多两个 Passage；安装 `ml` extra 后可以用 cross-encoder。cross-encoder 只处理几十个候选，避免全库推理成本。
5. **摘要回退**：全文库为空或没有命中时仍使用元数据摘要，保证系统可降级。回答必须明确证据粒度，不能把摘要证据包装成全文实验结论。
6. **数据库维度是迁移契约**：当前演示 embedding 为确定性 256 维 hash 向量。替换成科学 embedding 时必须同时新增数据库迁移，不允许运行时静默改变维度。
7. **瞬时错误有界重试**：OpenAlex、Crossref 和 Semantic Scholar 只对网络错误、超时、429、408、425 与特定 5xx 重试。退避遵循 `Retry-After` 并加入随机抖动；400、401、403、404 等永久性错误立即返回，避免重试风暴。

相关环境变量：

- `RESEARCH_AGENT_PROVIDER_MAX_ATTEMPTS`：包含首次请求在内的最大尝试次数，默认 `3`；
- `RESEARCH_AGENT_PROVIDER_INITIAL_BACKOFF_SECONDS`：首次退避，默认 `0.25` 秒；
- `RESEARCH_AGENT_PROVIDER_MAX_BACKOFF_SECONDS`：最大退避，默认 `4` 秒。

Prometheus 额外暴露 Provider attempt/retry、单次 attempt 耗时、工作流阶段耗时、模型
首 Token 和模型总耗时。指标标签只包含有限枚举值，不记录查询、论文内容或租户数据。

## 配置矩阵

| 场景 | provider | index | reranker | 用途 |
|---|---|---|---|---|
| CI/面试演示 | `offline` | `memory` | `lexical` | 零密钥、确定性复现 |
| 本地全文演示 | `offline` | `postgres` | `lexical` | 展示 GROBID、GIN、HNSW、RRF |
| 联网研究 | `hybrid` | `postgres` | `lexical` | OpenAlex/Crossref，可选 Semantic Scholar |
| 质量实验 | `hybrid` | `postgres` | `cross_encoder` | 在标注集上比较 Recall 与 nDCG |

## 本地验证

```bash
docker compose up -d postgres
python -m pip install -e ".[dev,postgres]"
POSTGRES_TEST_DSN=postgresql://research:research@127.0.0.1:5432/research_agent \
  pytest tests/test_postgres_index.py
```

Windows PowerShell：

```powershell
$env:POSTGRES_TEST_DSN="postgresql://research:research@127.0.0.1:5432/research_agent"
pytest tests/test_postgres_index.py
```

## 生产前必须补齐

- 使用领域 embedding 和自有 query-passage 标注集，而不是把某个公开模型名称当作质量保证。
- 为 PDF 上传增加租户鉴权、恶意文件扫描、对象存储、幂等键和异步任务队列。
- 将数据库迁移交给 Alembic 等显式迁移工具；应用启动账户不应拥有 `CREATE EXTENSION` 权限。
- GROBID 继续统一到相同退避策略，并为远端 Provider 增加熔断、缓存与速率预算。
- 评测解析覆盖率、Recall@K、nDCG@K、来源多样性、citation precision 和端到端支持率。
- 记录模型/索引/语料版本，但避免把受版权或敏感全文写入普通日志。

## 参考实现依据

- [GROBID service API](https://grobid.readthedocs.io/en/latest/Grobid-service/)
- [GROBID TEI encoding](https://grobid.readthedocs.io/en/latest/TEI-encoding-of-results/)
- [pgvector-python](https://github.com/pgvector/pgvector-python)
- [Semantic Scholar Academic Graph API](https://www.semanticscholar.org/product/api)
