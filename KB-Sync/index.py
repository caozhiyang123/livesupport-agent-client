import os

import boto3

bedrock_agent = boto3.client("bedrock-agent")

KB_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_ID = os.environ["DATA_SOURCE_ID"]


def handler(event, context):
    """Triggered by an EventBridge rule on S3 Object Created/Deleted events.

    Starts a Bedrock Knowledge Base ingestion job so that changes uploaded
    to S3 are re-synced into the KB. Skips starting a new job if one is
    already in progress, since StartIngestionJob rejects concurrent jobs
    for the same data source (ConflictException).
    """
    jobs = bedrock_agent.list_ingestion_jobs(
        knowledgeBaseId=KB_ID,
        dataSourceId=DATA_SOURCE_ID,
        filters=[{"attribute": "STATUS", "operator": "EQ", "values": ["IN_PROGRESS"]}],
    )
    if jobs.get("ingestionJobSummaries"):
        print("Ingestion job already in progress, skipping.")
        return {"status": "skipped"}

    response = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=KB_ID,
        dataSourceId=DATA_SOURCE_ID,
        description="Triggered by S3 event via EventBridge",
    )
    job_id = response["ingestionJob"]["ingestionJobId"]
    print(f"Started ingestion job: {job_id}")
    return {"status": "started", "jobId": job_id}
