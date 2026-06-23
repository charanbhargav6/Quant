import os
import requests
import boto3
from datetime import datetime, timezone

def run_watchdog():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        print("Missing Supabase credentials. Cannot check heartbeat.")
        return

    # Check Supabase for the latest system status
    try:
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }
        resp = requests.get(
            f"{supabase_url}/rest/v1/crave_system_status?select=last_heartbeat,bot_running,active_node&limit=1",
            headers=headers,
            timeout=10
        )
        data = resp.json()
        if not data:
            print("No system status found in Supabase.")
            return
            
        status = data[0]
        last_hb = datetime.fromisoformat(status["last_heartbeat"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_seconds = (now - last_hb).total_seconds()
        
        print(f"Latest heartbeat was at {last_hb} (Age: {age_seconds:.0f}s)")
        
        # If heartbeat is older than 5 minutes (300 seconds), assume dead
        if age_seconds > 300:
            print("🚨 HEARTBEAT DEAD! Attempting to start AWS fallback instance...")
            _start_aws()
        else:
            print("✅ Heartbeat is fresh. System is healthy.")
            
    except Exception as e:
        print(f"Error checking heartbeat: {e}")

def _start_aws():
    try:
        ec2 = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
        
        # Find CRAVE-bot instance
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": ["CRAVE-bot"]},
            {"Name": "instance-state-name", "Values": ["stopped"]}
        ])
        
        instance_id = None
        for r in resp.get("Reservations", []):
            for i in r.get("Instances", []):
                instance_id = i["InstanceId"]
                break
                
        if not instance_id:
            print("No stopped CRAVE-bot instance found.")
            return
            
        print(f"Starting instance {instance_id}...")
        ec2.start_instances(InstanceIds=[instance_id])
        print("✅ AWS instance start command sent successfully!")
        
    except Exception as e:
        print(f"Failed to start AWS instance: {e}")

if __name__ == "__main__":
    run_watchdog()
