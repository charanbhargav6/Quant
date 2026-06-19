"""
CRAVE v10.0 — AWS Manager
===========================
Controls EC2 instances for the tertiary node (cloud backup).
Designed for AWS Student credits — minimises cost by only
running the instance when laptop and phone are unavailable.

COST OPTIMISATION:
  t3.micro (always on):  ~$7.50/month — orchestrator heartbeat
  t3.small (on-demand):  starts on failover, stops when primary resumes
  With $100 student credits → ~10 months of coverage

SETUP:
  1. Apply at education.github.com/pack → AWS Educate
  2. Create IAM user with EC2StartStop policy (see SETUP_GUIDE.md)
  3. Set in .env:
       AWS_ACCESS_KEY_ID=...
       AWS_SECRET_ACCESS_KEY=...
       AWS_REGION=ap-south-1  (Mumbai — low latency from India)
       AWS_KEY_NAME=crave-key  (your EC2 key pair name)
  4. Run: python -c "from infra.aws_manager import aws; aws.setup()"

USAGE:
  from infra.aws_manager import aws

  aws.start_instance()   # spin up t3.small
  aws.stop_instance()    # shut down t3.small (save credits)
  aws.get_status()       # instance state + cost estimate
  aws.get_credits_used() # approximate credits consumed
"""

import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("crave.aws")


class AWSManager:

    # Instance config — matches config.py AWS section
    INSTANCE_TYPE_MICRO = "t3.micro"   # Always-on orchestrator ($7.50/mo)
    INSTANCE_TYPE_SMALL = "t3.small"   # On-demand bot node ($15/mo if always on)

    # Cost per hour (USD) — approximate
    COST_PER_HOUR = {
        "t3.micro": 0.0104,
        "t3.small": 0.0208,
    }

    def __init__(self):
        self._region          = os.environ.get("AWS_REGION", "ap-south-1")
        self._key_name        = os.environ.get("AWS_KEY_NAME", "crave-key")
        self._instance_id:    Optional[str] = None   # loaded on first call
        self._instance_start: Optional[datetime] = None
        self._ec2             = None
        self._available       = False
        self._connect()

    def _connect(self) -> bool:
        """Initialise boto3 EC2 client."""
        if self._available and self._ec2 is not None:
            return True
            
        try:
            import boto3
            self._ec2       = boto3.client(
                "ec2",
                region_name=self._region,
                aws_access_key_id     = os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY"),
            )
            # Quick connectivity test
            self._ec2.describe_regions(RegionNames=[self._region])
            self._available = True
            logger.info(f"[AWS] Connected — region: {self._region}")
            return True
        except ImportError:
            logger.info("[AWS] boto3 not installed. Run: pip install boto3")
        except Exception as e:
            logger.info(f"[AWS] Not available: {e}")
        return False

    def _ensure_connected(self) -> bool:
        return self._connect()

    # ─────────────────────────────────────────────────────────────────────────
    # INSTANCE DISCOVERY
    # ─────────────────────────────────────────────────────────────────────────

    def _find_crave_instance(self) -> Optional[str]:
        """Find existing CRAVE instance by tag."""
        if not self._available:
            return None

        try:
            resp = self._ec2.describe_instances(Filters=[
                {"Name": "tag:Name",  "Values": ["CRAVE-bot"]},
                {"Name": "instance-state-name",
                 "Values": ["pending", "running", "stopped", "stopping"]},
            ])
            reservations = resp.get("Reservations", [])
            for r in reservations:
                for inst in r.get("Instances", []):
                    return inst["InstanceId"]
        except Exception as e:
            logger.error(f"[AWS] Instance discovery failed: {e}")
        return None

    def _get_instance_id(self) -> Optional[str]:
        """Get instance ID, discovering if not yet known."""
        if not self._instance_id:
            self._instance_id = self._find_crave_instance()
        return self._instance_id

    # ─────────────────────────────────────────────────────────────────────────
    # INSTANCE CONTROL
    # ─────────────────────────────────────────────────────────────────────────

    def start_instance(self, wait: bool = False) -> bool:
        """
        Start the CRAVE EC2 instance.
        Returns True if started successfully.
        wait=True blocks until instance is running (~45-60 seconds).
        """
        if not self._ensure_connected():
            logger.warning("[AWS] Not available — cannot start instance.")
            return False

        instance_id = self._get_instance_id()
        if not instance_id:
            logger.warning("[AWS] No CRAVE instance found. Run aws.setup() first.")
            return False

        try:
            state = self._get_state()

            if state == "running":
                logger.info("[AWS] Instance already running.")
                return True

            if state in ("stopping", "shutting-down"):
                logger.info("[AWS] Instance stopping — waiting before start...")
                time.sleep(15)

            logger.info(f"[AWS] Starting instance {instance_id}...")
            self._ec2.start_instances(InstanceIds=[instance_id])
            self._instance_start = datetime.now(timezone.utc)

            if wait:
                return self._wait_for_state("running", timeout_secs=120)

            # Notify
            try:
                from interfaces.telegram_interface import tg
                tg.send(
                    f"☁️ <b>AWS Instance Starting</b>\n"
                    f"ID: {instance_id}\n"
                    f"Region: {self._region}\n"
                    f"Est. ready in: ~60 seconds"
                )
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"[AWS] Start failed: {e}")
            return False

    def stop_instance(self) -> bool:
        """
        Stop the CRAVE EC2 instance to save student credits.
        Only stops the t3.small bot instance, not t3.micro orchestrator.
        Called automatically when primary node (laptop/phone) resumes.
        """
        if not self._available:
            return False

        instance_id = self._get_instance_id()
        if not instance_id:
            return False

        try:
            state = self._get_state()

            if state == "stopped":
                logger.info("[AWS] Instance already stopped.")
                return True

            if state != "running":
                logger.info(f"[AWS] Instance in state {state} — not stopping.")
                return False

            logger.info(f"[AWS] Stopping instance {instance_id}...")
            self._ec2.stop_instances(InstanceIds=[instance_id])

            # Calculate runtime cost
            if self._instance_start:
                hours = (
                    datetime.now(timezone.utc) - self._instance_start
                ).total_seconds() / 3600
                cost  = hours * self.COST_PER_HOUR[self.INSTANCE_TYPE_SMALL]
                logger.info(
                    f"[AWS] Instance stopped. "
                    f"Runtime: {hours:.1f}h | Cost: ${cost:.4f}"
                )

            # Notify
            try:
                from interfaces.telegram_interface import tg
                tg.send(
                    f"☁️ <b>AWS Instance Stopped</b>\n"
                    f"Primary node has resumed.\n"
                    f"Credits saved. ✅"
                )
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"[AWS] Stop failed: {e}")
            return False

    def _get_state(self) -> str:
        """Get current instance state."""
        instance_id = self._get_instance_id()
        if not instance_id or not self._available:
            return "unknown"

        try:
            resp  = self._ec2.describe_instance_status(
                InstanceIds=[instance_id],
                IncludeAllInstances=True,
            )
            statuses = resp.get("InstanceStatuses", [])
            if statuses:
                return statuses[0]["InstanceState"]["Name"]
        except Exception as e:
            logger.debug(f"[AWS] State check failed: {e}")
        return "unknown"

    def _wait_for_state(self, target: str, timeout_secs: int = 120) -> bool:
        """Block until instance reaches target state or timeout."""
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            state = self._get_state()
            if state == target:
                logger.info(f"[AWS] Instance is now {target}.")
                return True
            logger.debug(f"[AWS] Waiting for {target}... (current: {state})")
            time.sleep(10)
        logger.warning(f"[AWS] Timeout waiting for {target}.")
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # SETUP (one-time)
    # ─────────────────────────────────────────────────────────────────────────

    def setup(self):
        """
        One-time setup: create EC2 instance with CRAVE configuration.
        Includes: Ubuntu 22.04, Python 3.11, all dependencies.
        Run once after getting AWS student credits.
        """
        if not self._available:
            print("❌ AWS not configured. Set AWS keys in .env first.")
            return

        # Check if instance already exists
        if self._find_crave_instance():
            print(f"✅ CRAVE instance already exists: {self._instance_id}")
            return

        from config.config import AWS as AWS_CFG

        # User data script — runs on first boot
        user_data = """#!/bin/bash
set -e
apt-get update -qq
apt-get install -y -qq python3 python3-pip git

# Clone CRAVE
git clone https://github.com/charanbhargav6/CRAVE.git /home/ubuntu/CRAVE
cd /home/ubuntu/CRAVE

# Install dependencies
pip3 install -q pandas numpy requests python-telegram-bot python-dotenv \\
    ccxt yfinance scikit-learn schedule pytz websocket-client boto3 gitpython psutil

# Create systemd service
cat > /etc/systemd/system/crave.service << 'SERVICE'
[Unit]
Description=CRAVE Trading Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/CRAVE
ExecStart=/usr/bin/python3 run_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl enable crave
echo "CRAVE setup complete."
"""

        try:
            resp = self._ec2.run_instances(
                ImageId      = AWS_CFG.get("ami_id", "ami-0f58b397bc5c1f2e8"),
                InstanceType = self.INSTANCE_TYPE_SMALL,
                MinCount     = 1,
                MaxCount     = 1,
                KeyName      = self._key_name,
                UserData     = user_data,
                TagSpecifications=[{
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name",    "Value": "CRAVE-bot"},
                        {"Key": "Project", "Value": "CRAVE"},
                    ]
                }],
            )

            instance_id         = resp["Instances"][0]["InstanceId"]
            self._instance_id   = instance_id

            print(f"✅ EC2 instance created: {instance_id}")
            print(f"   Region: {self._region}")
            print(f"   Type: {self.INSTANCE_TYPE_SMALL}")
            print(f"   Waiting for instance to start...")

            self._wait_for_state("running", timeout_secs=180)

            # Get public IP
            resp2 = self._ec2.describe_instances(InstanceIds=[instance_id])
            ip    = (resp2["Reservations"][0]["Instances"][0]
                     .get("PublicIpAddress", "N/A"))

            print(f"\n✅ Instance running!")
            print(f"   Public IP: {ip}")
            print(f"   SSH: ssh -i {self._key_name}.pem ubuntu@{ip}")
            print(f"\nNext: copy your .env to the instance:")
            print(f"   scp .env ubuntu@{ip}:~/CRAVE/.env")
            print(f"   ssh -i {self._key_name}.pem ubuntu@{ip}")
            print(f"   cd CRAVE && python run_bot.py")

        except Exception as e:
            print(f"❌ Setup failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS & COST TRACKING
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Current instance state and cost estimate."""
        state       = self._get_state()
        instance_id = self._get_instance_id()

        runtime_h    = 0.0
        session_cost = 0.0

        if self._instance_start and state == "running":
            runtime_h    = (
                datetime.now(timezone.utc) - self._instance_start
            ).total_seconds() / 3600
            session_cost = runtime_h * self.COST_PER_HOUR[self.INSTANCE_TYPE_SMALL]

        return {
            "available":     self._available,
            "instance_id":   instance_id or "not configured",
            "state":         state,
            "region":        self._region,
            "runtime_hours": round(runtime_h, 2),
            "session_cost":  f"${session_cost:.4f}",
        }

    def get_status_message(self) -> str:
        """Formatted status for Telegram."""
        s = self.get_status()
        if not s["available"]:
            return (
                "☁️ <b>AWS STATUS</b>\n"
                "Not configured.\n"
                "Set AWS keys in .env to enable."
            )
        state_emoji = {
            "running": "✅", "stopped": "⏹️",
            "pending": "⏳", "stopping": "🔄",
            "unknown": "❓"
        }.get(s["state"], "❓")
        return (
            f"☁️ <b>AWS STATUS</b>\n"
            f"Instance : {s['instance_id']}\n"
            f"State    : {state_emoji} {s['state']}\n"
            f"Region   : {s['region']}\n"
            f"Runtime  : {s['runtime_hours']}h\n"
            f"Cost     : {s['session_cost']} this session"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 5 — Lazy singleton (boto3 may not be installed; crash-on-import breaks
# entire bot even in paper mode where AWS is never used)
# ─────────────────────────────────────────────────────────────────────────────

_aws_instance: Optional["AWSManager"] = None

def get_aws() -> "AWSManager":
    global _aws_instance
    if _aws_instance is None:
        _aws_instance = AWSManager()
    return _aws_instance


# Backward compat alias
aws = get_aws
