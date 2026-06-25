import sys
import os
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')

from infra.aws_manager import get_aws
aws = get_aws()
status = aws.get_status()
print("AWS State:", status.get('state'))
print("Instance ID:", status.get('instance_id'))
