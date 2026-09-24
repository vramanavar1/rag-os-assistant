# KQL snippets (Application Insights / Log Analytics)

RAG-OS writes structured JSON logs (every line carries `correlation_id`) and OpenTelemetry traces and metrics.
Run these in **Application Insights → Logs**. Swap `<cid>` for the `X-Correlation-ID` returned by the API
or shown in the chat UI.

## Trace one request end to end
```kusto
union requests, dependencies, traces, exceptions
| where timestamp > ago(1d)
| where customDimensions.correlation_id == "<cid>" or customDimensions["rag.correlation_id"] == "<cid>"
| project timestamp, itemType, name, message, duration, resultCode, cloud_RoleName
| order by timestamp asc
```

## Tokens per query and per purpose (answer / condense / classify / embeddings)
```kusto
customMetrics
| where timestamp > ago(7d) and name == "rag.tokens"
| extend kind = tostring(customDimensions.kind), purpose = tostring(customDimensions.purpose),
         provider = tostring(customDimensions.provider), model = tostring(customDimensions.model)
| summarize tokens = sum(valueSum) by bin(timestamp, 1h), kind, purpose, provider, model
| order by timestamp desc
```

## Stage latency p50/p95 (embed_query, search, llm, ingestion stages)
```kusto
customMetrics
| where timestamp > ago(1d) and name == "rag.stage.duration"
| extend stage = tostring(customDimensions.stage)
| summarize p50 = percentile(valueSum / valueCount, 50), p95 = percentile(valueSum / valueCount, 95) by stage
```

## Ingestion outcomes per source
```kusto
customMetrics
| where timestamp > ago(1d) and name == "rag.ingest.docs"
| summarize docs = sum(valueSum) by tostring(customDimensions.source_id), tostring(customDimensions.status), bin(timestamp, 15m)
```

## Documents that failed permanently or were dead-lettered
```kusto
traces
| where timestamp > ago(1d)
| where message in ("document failed permanently", "document dead-lettered")
| project timestamp, doc_id = tostring(customDimensions.doc_id), error_type = tostring(customDimensions.error_type),
          error = tostring(customDimensions.error)
| order by timestamp desc
```

## Access-control audit: admin bypasses
```kusto
traces
| where timestamp > ago(30d) and message == "admin access bypass"
| project timestamp, subject = tostring(customDimensions.subject), issuer = tostring(customDimensions.issuer)
```

## Embedding profile guard failures (model / revision / dimension mismatch)
```kusto
traces
| where timestamp > ago(1d) and message has "embedding profile guard failed"
| project timestamp, cloud_RoleName, reasons = tostring(customDimensions.reasons)
```

## Suggested alerts
| Signal | Condition |
|---|---|
| Service Bus dead-letter count | `DeadletteredMessages > 0` on either queue |
| Ingestion failure rate | FAILED / processed > 2% over 30 min (from `rag.ingest.docs`) |
| Retrieval latency | p95 of `rag.stage.duration` where stage = `rag.chat` above the SLO |
| Queue age | Service Bus `ActiveMessages` growing for 30 min while workers are at max replicas |
| Profile guard | any "embedding profile guard failed" trace |
