# GOAL
-   Build Basic MVP to understand how Data from Relational database (Semantic Model), Taxonomy and Ontology come together to address below "Use Case 1"

# Use Case 1: mortgage loss mitigation document chase
## The decision
-   A homeowner falls behind and applies for hardship assistance. The servicer must determine whether the
    submitted package is complete against the relevant investor guideline, then state precisely what is missing. The judgment is completeness against a specific version of a specific guide.

##  The clock
-   Under Regulation X, 12 CFR 1024.41, the servicer has five business days from receipt 
    to acknowledge the application and identify the missing documents. An incorrect missing-items list restarts the clock.

# Steps
-   Create simple relational table(s) in existing database for this domain with some sample data with uc1 prefix; later to delete
-   What, How and Where the Taxonomy of this domain is going to be prepared and stored
-   Construction of Ontology for this domain as basic one to address this use case
-   How relational table(s), Taxonomy and Ontology come together to address this
-   Create docs\UseCase1.Html with diagrams as needed and with table of contents, Vocabulary, Abbreviations of domain etc
-   Some Tests; perhaps this is sort of multi-agents doing the job? Not sure; please advise with best practices

# Implementation Note
- Let us first create docs\UseCase1.Html for review as how exactly this is done from both theoritically and architecture design; before any code changes
-   Consider using https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance as guidance

# NOTE
-   Keep track of the changes for "Use Case 1" to later delete; to keep this project clean