"""Bounded non-authoritative timings from the real package-manager output stream."""
import re
import time
from urllib.parse import urlsplit
from verification_observability import emit_observability_event, new_observability_id

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CLASSIC = re.compile(r"\[([1-4])/4\] (Resolving packages|Fetching packages|Linking dependencies|Building fresh packages)")
BERRY = re.compile(r"[┌+] (Resolution|Fetch|Link) step")
NETWORK = re.compile(r"\b(ESOCKETTIMEDOUT|ETIMEDOUT|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ENETUNREACH)\b")

class PackageManagerTelemetry:
    def __init__(self, label, *, clock=time.monotonic, emit=emit_observability_event):
        self.label, self.clock, self.emit = label, clock, emit
        self.operation = new_observability_id("package-manager-phases")
        self.buffer = ""; self.active = None; self.started = None
        self.phases = []; self.network_errors = {}; self.hosts = set()
        self.cache_hits = self.cache_misses = self.build_messages = 0
        self.cache_evidence = False

    def consume(self, chunk):
        # Reader chunks and carriage-return repainting are not line boundaries.
        self.buffer += ANSI.sub("",str(chunk)).replace("\r","\n")
        while "\n" in self.buffer:
            line,self.buffer=self.buffer.split("\n",1);self._line(line)
        self.buffer=self.buffer[-16384:]

    def _close(self, *, reported_ms=None):
        if self.active is None:return
        row={"phase":self.active,"observedMs":max(0,round((self.clock()-self.started)*1000)),
             "reportedMs":reported_ms}
        self.phases.append(row)
        self.emit("package-manager.phase.finish",operationId=self.operation,label=self.label,
                  authority="PERFORMANCE_ONLY",**row)
        self.active=None;self.started=None

    def _line(self, line):
        phase=None
        classic=CLASSIC.search(line);berry=BERRY.search(line)
        if classic:phase=("resolve","fetch","link","build")[int(classic.group(1))-1]
        elif berry:phase={"Resolution":"resolve","Fetch":"fetch","Link":"link"}[berry.group(1)]
        if phase:
            self._close();self.active=phase;self.started=self.clock()
            self.emit("package-manager.phase.start",operationId=self.operation,label=self.label,
                      phase=phase,authority="PERFORMANCE_ONLY")
        elif self.active and "Completed in " in line and ("└" in line or "YN0000" in line):
            text=line.split("Completed in ",1)[1]
            units=re.findall(r"(\d+)\s*(ms|m|s)\b",text)
            ms=sum(int(n)*{"m":60000,"s":1000,"ms":1}[unit] for n,unit in units) if units else None
            self._close(reported_ms=ms)
        if "can't be found in the cache" in line or "will be fetched from the remote" in line:
            self.cache_misses+=1;self.cache_evidence=True
        if "already cached" in line or "retrieved from cache" in line:
            self.cache_hits+=1;self.cache_evidence=True
        if "YN0007:" in line or "YN0008:" in line:self.build_messages+=1
        for error in NETWORK.findall(line):self.network_errors[error]=self.network_errors.get(error,0)+1
        if NETWORK.search(line) or "retrying" in line.lower():
            for url in re.findall(r"https?://[^\s\"']+",line):
                try:
                    host=urlsplit(url).hostname
                    if host and len(self.hosts)<20:self.hosts.add(host)
                except ValueError:pass

    def finish(self, returncode):
        if self.buffer:self._line(self.buffer);self.buffer=""
        self._close()
        summary={"phases":list(self.phases),"reportedCacheHitMessages":self.cache_hits if self.cache_evidence else None,
                 "reportedCacheMissMessages":self.cache_misses if self.cache_evidence else None,
                 "buildMessages":self.build_messages,"networkErrors":dict(self.network_errors),
                 "networkHosts":sorted(self.hosts),"returncode":returncode,"authority":"PERFORMANCE_ONLY"}
        if self.phases or self.network_errors or self.cache_evidence:
            self.emit("package-manager.phases.summary",operationId=self.operation,label=self.label,**summary)
        return summary
