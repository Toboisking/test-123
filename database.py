import json
import httpx
import logging
import base64

log = logging.getLogger(__name__)

# Store database.json on a dedicated branch so the bot's own frequent
# 'Update database [skip ci]' commits never trigger a Railway redeploy
# (Railway watches only the default 'main' branch).
DB_BRANCH = "data"

class RepoDB:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
        self.data = {"approved": [], "banned": [], "subscriptions": {}, "names": {}, "daily_usage": {}, "free_mode": False, "total_files": 0}
        self.file_sha = None
        if self.token and self.repo:
            self._ensure_data_branch()
            self._load()

    def _ensure_data_branch(self):
        try:
            r = httpx.get(f"https://api.github.com/repos/{self.repo}/git/ref/heads/{DB_BRANCH}", headers=self.headers, timeout=10)
            if r.status_code == 200:
                return
            r = httpx.get(f"https://api.github.com/repos/{self.repo}/git/ref/heads/main", headers=self.headers, timeout=10)
            if r.status_code == 200:
                sha = r.json()["object"]["sha"]
                httpx.post(
                    f"https://api.github.com/repos/{self.repo}/git/refs",
                    headers=self.headers,
                    json={"ref": f"refs/heads/{DB_BRANCH}", "sha": sha},
                    timeout=10,
                )
        except Exception as e:
            log.error(f"RepoDB ensure data branch error: {e}")

    def _load(self):
        try:
            r = httpx.get(f"https://api.github.com/repos/{self.repo}/contents/database.json?ref={DB_BRANCH}", headers=self.headers, timeout=10)
            if r.status_code == 200:
                self.file_sha = r.json()["sha"]
                content = base64.b64decode(r.json()["content"]).decode("utf-8")
                loaded = json.loads(content)
                self.data["approved"] = loaded.get("approved", [])
                self.data["banned"] = loaded.get("banned", [])
                self.data["subscriptions"] = loaded.get("subscriptions", {})
                self.data["names"] = loaded.get("names", {})
                self.data["daily_usage"] = loaded.get("daily_usage", {})
                self.data["free_mode"] = loaded.get("free_mode", False)
                self.data["total_files"] = loaded.get("total_files", 0)
        except Exception as e:
            log.error(f"RepoDB load error: {e}")

    def save(self):
        if not self.token or not self.repo: return
        try:
            content_str = json.dumps(self.data, indent=2)
            content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
            payload = {
                "message": "Update database [skip ci]",
                "content": content_b64,
                "branch": DB_BRANCH
            }
            if self.file_sha:
                payload["sha"] = self.file_sha
            
            r = httpx.put(f"https://api.github.com/repos/{self.repo}/contents/database.json", headers=self.headers, json=payload, timeout=10)
            if r.status_code in (200, 201):
                self.file_sha = r.json()["content"]["sha"]
            else:
                log.error(f"RepoDB save failed: {r.status_code} {r.text}")
        except Exception as e:
            log.error(f"RepoDB save error: {e}")

    def add_approved(self, uid: str, name: str = ""):
        if uid not in self.data["approved"]:
            self.data["approved"].append(uid)
        if uid in self.data["banned"]:
            self.data["banned"].remove(uid)
        if name:
            self.data["names"][uid] = name
        self.save()

    def remove_approved(self, uid: str):
        if uid in self.data["approved"]:
            self.data["approved"].remove(uid)
        self.save()

    def ban(self, uid: str, name: str = ""):
        if uid not in self.data["banned"]:
            self.data["banned"].append(uid)
        if uid in self.data["approved"]:
            self.data["approved"].remove(uid)
        if name:
            self.data["names"][uid] = name
        self.save()

    def unban(self, uid: str):
        if uid in self.data["banned"]:
            self.data["banned"].remove(uid)
        self.save()

    def set_sub(self, uid: str, expires_at: str, daily_limit: int, name: str = ""):
        self.data["subscriptions"][uid] = {"expires_at": expires_at, "daily_limit": daily_limit}
        if name:
            self.data["names"][uid] = name
        self.save()

    def remove_sub(self, uid: str):
        if uid in self.data["subscriptions"]:
            del self.data["subscriptions"][uid]
        self.save()

    def get_name(self, uid: str):
        return self.data["names"].get(uid, "Unknown")
