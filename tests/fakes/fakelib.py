import hashlib, json, os
REG_STATE = os.environ.get("FAKE_REG_STATE", "")
def dg(b): return "sha256:" + hashlib.sha256(b).hexdigest()
def load_reg():
    with open(REG_STATE) as f: return json.load(f)
def objects(repo, tag):
    """Return (index_digest, blobs{digest: (bytes, mediatype)}, labels) for repo:tag."""
    ent = load_reg()[repo][tag]
    cfg = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Labels": ent["labels"]}}).encode()
    man = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                      "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": dg(cfg), "size": len(cfg)},
                      "layers": []}).encode()
    idx = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                      "manifests": [
                          {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": dg(b"arm" + man), "platform": {"os": "linux", "architecture": "arm64"}},
                          {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": dg(man), "size": len(man), "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
    blobs = {dg(cfg): (cfg, "application/octet-stream"), dg(man): (man, "application/vnd.oci.image.manifest.v1+json"),
             dg(idx): (idx, "application/vnd.oci.image.index.v1+json")}
    return dg(idx), blobs, ent["labels"]
