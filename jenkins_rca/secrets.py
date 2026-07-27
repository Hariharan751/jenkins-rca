"""Load runtime config from AWS Secrets Manager.

Secret structure (JSON) at SecretId `jenkins-rca/prod`:
{
  "jenkins_url": "http://10.34.42.254:8080",
  "jenkins_user": "...",
  "jenkins_token": "...",
  "webhook_secret": "...",
  "llm_provider": "openai",
  "llm_api_key": "sk-...",
  "github_pat": "ghp_..."
}

Bbctl-ec2 needs IAM role with `secretsmanager:GetSecretValue` on the secret ARN.
"""
import json
import os
import boto3


DEFAULT_SECRET_ID = "jenkins-rca/prod"
DEFAULT_REGION = "ap-south-1"


def load_secrets(
    secret_id: str | None = None,
    region: str | None = None,
) -> dict:
    secret_id = secret_id or os.environ.get("JENKINS_RCA_SECRET_ID", DEFAULT_SECRET_ID)
    region = region or os.environ.get("AWS_REGION", DEFAULT_REGION)
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_id)
    return json.loads(resp["SecretString"])


def export_env() -> None:
    """Print `export JENKINS_RCA_<KEY>=<value>` lines for sourcing in shell."""
    for k, v in load_secrets().items():
        # bash-escape single quotes in value
        safe = str(v).replace("'", "'\\''")
        print(f"export JENKINS_RCA_{k.upper()}='{safe}'")


if __name__ == "__main__":
    export_env()
