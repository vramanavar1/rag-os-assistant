#   GOAL
-   Knowledge assistant that replies to user queries accurately based on ground truth of the source repository

#   Features
-   Able to configure Taxonomy of the given Domain Knowledge dynamically by the Subject Matter Experts
-   Able to define Ontology of the selected Domain
-   Query access strictly restricted/filtered by the user's attribute Department, Region, EmployeeID etc (RBAC permissions)
-   Should be able to ingest documents from various sources (Local Harddrive storage, Google Drive, Sharepoint, FTP etc)
-   Also the formats could be pdf, docx, txt, .csv, .xlsx, .xls, .json, .xml, jsonp, jsonl etc
-   Source Embedding Model should be configurable either it should allow to use Locally configured one or from Remote Online
-   Both Ingestion and Query prompts should use exact same Embedding model and length to avoid mismatch
-   Each and every subsystem should log traces and detailed errors to be logged for easy troubleshooting
-   Observability should be built in to start with tokens used for each query
-   The User Query/Chat should expose an URL to be embedded in Customer Portal with suitable context being injected for RBAC permissions

#   Plan
-   Need to host this solution on Microsoft Foundry (New Foundry)
-   Follow SOLID, Clean Architecture principles
-   Suggest/Recommend best technology stack to be used