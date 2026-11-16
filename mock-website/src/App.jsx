import React, { useEffect, useMemo, useState } from 'react'

const RPO_OPTIONS = [
  { label: '5 minutes', value: '5m', binlog: 'every 5 minutes', full: 'daily 01:00', worst: '5 minutes of data', note: 'For systems where every transaction counts: orders, payments, bookings. Transaction logs ship every 5 minutes, so the worst case is 5 minutes of data lost.' },
  { label: '15 minutes', value: '15m', binlog: 'every 15 minutes', full: 'daily 01:00', worst: '15 minutes of data', note: 'A common middle ground for business systems. Transaction logs ship every 15 minutes, so the worst case is 15 minutes of data lost.' },
  { label: '1 hour', value: '1h', binlog: 'hourly', full: 'daily 01:00', worst: '1 hour of data', note: 'For internal tools and reporting databases where an hour of rework is acceptable. Transaction logs ship hourly, so the worst case is 1 hour of data lost.' },
  { label: '24 hours', value: '24h', binlog: 'with the daily backup', full: 'daily 01:00', worst: '24 hours of data', note: 'For archives and low-change data. One full backup a day, no separate log shipping, so the worst case is 24 hours of data lost.' },
]

const SOURCES = [
  { id: 'mysql-prod', name: 'mysql-prod', engine: 'MySQL 8.4', detail: 'Group Replication, 3 nodes', host: '192.168.8.56', size: '28 GB', lastBackup: 'Today 01:00', status: 'Verified', verifiedAt: '13 Nov 2026, 01:42 IST', restoreTime: '6 min 12 s', rpo: '5m' },
  { id: 'pg-orders', name: 'pg-orders', engine: 'PostgreSQL 16', detail: 'Primary + 1 replica', host: '192.168.8.61', size: '41 GB', lastBackup: 'Today 01:00', status: 'Verified', verifiedAt: '13 Nov 2026, 01:55 IST', restoreTime: '8 min 40 s', rpo: '15m' },
  { id: 'mongo-app', name: 'mongo-app', engine: 'MongoDB 7.0', detail: 'Replica set, 3 nodes', host: '192.168.8.63', size: '12 GB', lastBackup: 'Today 01:00', status: 'Phase 2', verifiedAt: 'Not yet in verification', restoreTime: '-', rpo: '1h' },
]

const TARGETS = [
  { id: 'restore-01', name: 'restore-01', detail: 'Standby server, same data centre', host: '192.168.8.70' },
  { id: 'restore-02', name: 'restore-02', detail: 'Fresh cloud VM, ap-south-1', host: '3.110.81.147' },
  { id: 'restore-test', name: 'restore-test', detail: 'Test VM, wiped after each proof run', host: '192.168.8.71' },
]

const BUCKETS = {
  minio: { label: 'MinIO', bucket: 'percona-mysql-backup', endpoint: 'minio.ldc.opstree.dev:9000', note: 'Self-hosted object storage in the local data centre.' },
  s3: { label: 'Amazon S3', bucket: 'verified-restore-backups', endpoint: 's3.ap-south-1.amazonaws.com', note: 'Managed object storage, Mumbai region.' },
  other: { label: 'Other S3-compatible', bucket: 'your-bucket-name', endpoint: 'your-endpoint:9000', note: 'Any S3-compatible store: Ceph, Wasabi, Cloudflare R2.' },
}

// Timeline ticks follow the selected RPO. The window runs from the 01:00
// full backup to Now (latest log shipped, 16:47). Each tick is one RPO
// interval, so a 5 minute RPO shows points every 5 minutes.
const DAY_START = 60 // 01:00, in minutes from midnight
const NOW_MINUTES = 16 * 60 + 47 // 16:47, latest log shipped
const RPO_STEP_MINUTES = { '5m': 5, '15m': 15, '1h': 60, '24h': 1440 }
const RPO_STEP_LABEL = { '5m': '5 minutes', '15m': '15 minutes', '1h': '1 hour', '24h': '24 hours' }

function minutesToLabel(total) {
  const h = Math.floor(total / 60)
  const m = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

function buildTimelinePoints(rpoValue) {
  const step = RPO_STEP_MINUTES[rpoValue] || 15
  const points = []
  let t = DAY_START
  while (t < NOW_MINUTES) {
    const mins = t - DAY_START
    const tx = Math.round(mins * 13.2)
    points.push({
      label: minutesToLabel(t),
      time: minutesToLabel(t),
      desc: t === DAY_START ? 'Full backup taken' : `${tx.toLocaleString('en-IN')} transactions replayed`,
    })
    t += step
  }
  points.push({ label: 'Now', time: '16:47', desc: 'Latest log shipped, 16:47' })
  return points
}

function Badge({ children, tone }) {
  const tones = {
    green: 'bg-emerald-100 text-emerald-800',
    indigo: 'bg-indigo-100 text-indigo-800',
    amber: 'bg-amber-100 text-amber-800',
    slate: 'bg-slate-200 text-slate-700',
  }
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone] || tones.slate}`}>
      {children}
    </span>
  )
}

function SectionHeading({ kicker, title, sub }) {
  return (
    <div className="mb-8">
      <p className="text-sm font-semibold uppercase tracking-wide text-indigo-600">{kicker}</p>
      <h2 className="mt-1 text-2xl font-bold text-slate-900 sm:text-3xl">{title}</h2>
      {sub && <p className="mt-2 max-w-3xl text-slate-600">{sub}</p>}
    </div>
  )
}

function Dashboard({ onContext }) {
  const [selected, setSelected] = useState(SOURCES[0])
  const [rpo, setRpo] = useState(RPO_OPTIONS[0])
  const [target, setTarget] = useState(TARGETS[0])
  const [point, setPoint] = useState(4)
  const [restoring, setRestoring] = useState(false)
  const [bucketKey, setBucketKey] = useState('minio')
  const [backingUp, setBackingUp] = useState(false)
  const [backupProgress, setBackupProgress] = useState(0)
  const [lastBackupLabel, setLastBackupLabel] = useState('Today 01:00')
  const [backupLog, setBackupLog] = useState([
    '01:00:12  Full backup mysql-prod started, dump 28 GB',
    '01:04:31  Dump complete, sha256 verified against manifest',
    '01:04:33  Uploaded to bucket percona-mysql-backup (MinIO)',
    '01:04:35  Transaction logs shipped, RPO 5 minutes',
    '01:15:00  Log ship complete, 214 transactions',
    '01:30:00  Log ship complete, 186 transactions',
  ])
  const [restoreLog, setRestoreLog] = useState([
    '01:00:12  Full backup mysql-prod started, dump 28 GB',
    '01:04:31  Dump complete, sha256 verified against manifest',
    '01:04:33  Uploaded to bucket percona-mysql-backup (MinIO)',
    '01:42:05  Proof run started on restore-test',
    '01:48:17  Dump loaded, 14 databases present',
    '01:48:19  Binlogs replayed to 01:42, 312 transactions applied',
    '01:48:20  Row counts match source, proof PASSED',
  ])

  const bucket = BUCKETS[bucketKey]

  // Report live context (source, RPO, bucket) up to the dashboard page
  // top bar, so it always shows what the dashboard is currently set to.
  useEffect(() => {
    if (onContext) {
      onContext({ source: selected.name, rpo: rpo.label, bucket: bucket.bucket })
    }
  }, [selected, rpo, bucketKey, onContext])

  const timelinePoints = useMemo(() => buildTimelinePoints(rpo.value), [rpo])
  const timelineLabels = useMemo(() => {
    const total = timelinePoints.length
    const gap = Math.max(1, Math.ceil(total / 5))
    const labels = []
    for (let i = 0; i < total; i += gap) labels.push({ ...timelinePoints[i], index: i })
    const last = total - 1
    if (labels[labels.length - 1].index !== last) labels.push({ ...timelinePoints[last], index: last })
    return labels
  }, [timelinePoints])
  useEffect(() => {
    setPoint(timelinePoints.length - 1)
  }, [timelinePoints])
  const chosen = timelinePoints[point] || timelinePoints[timelinePoints.length - 1]

  function runBackup() {
    if (backingUp) return
    setBackingUp(true)
    setBackupProgress(0)
    const now = new Date()
    const stamp = (addSeconds) => {
      const d = new Date(now.getTime() + addSeconds * 1000)
      return d.toTimeString().slice(0, 8)
    }
    const steps = [
      { pct: 8, line: `${stamp(0)}  Full backup started: ${selected.name}, ${selected.size} estimated` },
      { pct: 25, line: `${stamp(4)}  Dumping tables, ${selected.engine} on ${selected.host}` },
      { pct: 55, line: `${stamp(18)}  Dump in progress, compressing on the fly` },
      { pct: 78, line: `${stamp(31)}  Dump complete, sha256 verified against manifest` },
      { pct: 88, line: `${stamp(33)}  Uploading to bucket ${bucket.bucket} (${bucket.label})` },
      { pct: 96, line: `${stamp(40)}  Upload complete, ${selected.size} stored in ${bucket.bucket}` },
      { pct: 100, line: `${stamp(42)}  Transaction log shipped, RPO ${rpo.label}. Backup complete.` },
    ]
    let i = 0
    setBackupLog((prev) => [...prev, `--- Manual backup: ${selected.name} to ${bucket.bucket} ---`])
    const timer = setInterval(() => {
      if (i >= steps.length) {
        clearInterval(timer)
        setBackingUp(false)
        setLastBackupLabel(`Today ${stamp(42).slice(0, 5)}`)
        return
      }
      const step = steps[i]
      setBackupProgress(step.pct)
      setBackupLog((prev) => [...prev, step.line])
      i += 1
    }, 650)
  }

  function runRestore() {
    if (restoring) return
    setRestoring(true)
    const lines = [
      `${chosen.time}:00  Restore started: ${selected.name} to ${target.name} (${target.host})`,
      `${chosen.time}:01  Loading full backup from 01:00`,
      `${chosen.time}:02  Dump loaded, databases present`,
      `${chosen.time}:03  Replaying transaction logs up to ${chosen.time}`,
      `${chosen.time}:04  ${chosen.desc}`,
      `${chosen.time}:05  Row counts match source, restore complete`,
    ]
    let i = 0
    setRestoreLog((prev) => [...prev, `--- New restore: ${selected.name} to ${chosen.time} on ${target.name} ---`])
    const timer = setInterval(() => {
      if (i >= lines.length) {
        clearInterval(timer)
        setRestoring(false)
        return
      }
      const line = lines[i]
      setRestoreLog((prev) => [...prev, line])
      i += 1
    }, 450)
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center gap-2 rounded-xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
        <span className="rounded-lg bg-indigo-600 px-3 py-1.5 font-mono text-xs font-semibold text-white">SOURCE: {selected.name}</span>
        <span className="font-mono text-sm text-slate-400">→ backup →</span>
        <span className="rounded-lg bg-slate-900 px-3 py-1.5 font-mono text-xs font-semibold text-white">BUCKET: {bucket.bucket}</span>
        <span className="font-mono text-sm text-slate-400">→ restore →</span>
        <span className="rounded-lg bg-emerald-600 px-3 py-1.5 font-mono text-xs font-semibold text-white">TARGET: {target.name}</span>
        <span className="ml-auto hidden text-xs text-slate-500 lg:block">One pipeline, two halves: backup fills the bucket, restore reads from it.</span>
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
      <section className="overflow-hidden rounded-xl bg-white shadow-sm ring-1 ring-slate-200">
        <div className="border-t-4 border-indigo-600 bg-indigo-50 px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-base font-bold text-slate-900">Backup: source to storage</h3>
            <Badge tone="indigo">Backup flow</Badge>
          </div>
          <p className="mt-1 text-sm text-slate-600">Pick a source, choose the bucket, and run a backup. The full backup runs daily at 01:00, logs ship on your RPO interval.</p>
        </div>
        <div className="space-y-5 p-5">
        <div>
        <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Sources</h3>
        <div className="mt-3 space-y-2">
          {SOURCES.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelected(s)}
              className={`w-full rounded-lg border p-3 text-left transition ${selected.id === s.id ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500' : 'border-slate-200 hover:border-slate-300'}`}
            >
              <div className="flex items-center justify-between">
                <span className="font-mono text-sm font-semibold text-slate-900">{s.name}</span>
                <Badge tone={s.status === 'Verified' ? 'green' : 'amber'}>{s.status}</Badge>
              </div>
              <p className="mt-1 text-xs text-slate-500">{s.engine} | {s.detail}</p>
              <p className="text-xs text-slate-500">{s.host} | {s.size} | Last backup {s.lastBackup}</p>
            </button>
          ))}
        </div>
        <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
          <p className="font-semibold text-slate-800">Selected: {selected.name}</p>
          <p className="mt-1">Last proof: {selected.verifiedAt}</p>
          <p>Proof restore time: {selected.restoreTime}</p>
          <p>RPO: {selected.rpo}</p>
        </div>
        </div>

        <div>
          <h4 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Backup flow</h4>
          <div className="mt-3 rounded-lg bg-slate-50 p-4">
            <div className="flex items-center gap-2 sm:gap-3">
              <div className="shrink-0 rounded-lg bg-white px-3 py-2 font-mono text-xs font-semibold text-slate-900 ring-1 ring-slate-200 sm:text-sm">{selected.name}</div>
              <div className={`relative h-1 min-w-8 flex-1 rounded-full ${backingUp ? 'bg-indigo-200' : 'bg-slate-300'}`}>
                <span className={`flow-dot${backingUp ? ' flow-dot-fast' : ''}`} />
              </div>
              <div className={`shrink-0 rounded-lg bg-white px-3 py-2 font-mono text-xs font-semibold text-slate-900 ring-1 sm:text-sm ${backingUp ? 'ring-2 ring-indigo-500' : 'ring-slate-200'}`}>{bucket.bucket}</div>
            </div>
            <p className="mt-3 text-xs text-slate-500">
              {backingUp ? 'Backup running: data is flowing from the source to the bucket.' : `Idle: full backup daily 01:00, logs ship ${rpo.binlog}. Worst case loss stays at ${rpo.worst}.`}
            </p>
          </div>
        </div>

        <div>
          <h4 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Backup destination</h4>
          <div className="mt-3 flex flex-wrap gap-2">
            {Object.entries(BUCKETS).map(([key, b]) => (
              <button
                key={key}
                onClick={() => setBucketKey(key)}
                className={`rounded-full px-4 py-2 text-sm font-medium transition ${bucketKey === key ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
              >
                {b.label}
              </button>
            ))}
          </div>
          <div className="mt-3 rounded-lg bg-slate-50 p-4 text-sm">
            <p><span className="text-slate-500">Bucket: </span><span className="font-mono font-semibold text-slate-900">{bucket.bucket}</span></p>
            <p className="mt-1"><span className="text-slate-500">Endpoint: </span><span className="font-mono text-slate-800">{bucket.endpoint}</span></p>
            <p className="mt-1 text-xs text-slate-500">{bucket.note}</p>
          </div>
        </div>

        <div>
          <h4 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Schedule and last backup</h4>
          <div className="mt-3 grid grid-cols-3 gap-3 text-center text-xs sm:text-sm">
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Full backup</p><p className="mt-1 font-semibold text-slate-900">daily 01:00</p></div>
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Log shipping</p><p className="mt-1 font-semibold text-slate-900">{rpo.binlog}</p></div>
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Last backup</p><p className="mt-1 font-semibold text-slate-900">{lastBackupLabel}</p></div>
          </div>
          <p className="mt-3 text-xs text-slate-500">Last backup size: {selected.size} | Next full backup: tomorrow 01:00 | RPO {rpo.label} keeps worst case loss at {rpo.worst}.</p>
          {(backingUp || backupProgress > 0) && (
            <div className="mt-3">
              <div className="flex justify-between text-xs text-slate-500"><span>{backingUp ? 'Backup in progress' : 'Backup complete'}</span><span>{backupProgress}%</span></div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-slate-200">
                <div className="h-full rounded-full bg-indigo-600 transition-all duration-500" style={{ width: `${backupProgress}%` }} />
              </div>
            </div>
          )}
          <button
            onClick={runBackup}
            disabled={backingUp}
            className="mt-4 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-50"
          >
            {backingUp ? 'Backing up...' : `Run backup now: ${selected.name} to ${bucket.bucket}`}
          </button>
        </div>

        <div className="overflow-hidden rounded-xl bg-slate-900 shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-700 px-4 py-2">
            <span className="font-mono text-xs font-semibold uppercase tracking-wide text-slate-300">Backup log</span>
            <span className="font-mono text-xs text-emerald-400">live demo log</span>
          </div>
          <div className="log-scroll max-h-56 overflow-y-auto p-4 font-mono text-xs leading-relaxed text-slate-200">
            {backupLog.map((line, i) => (
              <p key={i}>{line}</p>
            ))}
          </div>
        </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl bg-white shadow-sm ring-1 ring-slate-200">
        <div className="border-t-4 border-emerald-600 bg-emerald-50 px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-base font-bold text-slate-900">Restore: storage to target</h3>
            <Badge tone="green">Restore flow</Badge>
          </div>
          <p className="mt-1 text-sm text-slate-600">Reads from the same bucket: {bucket.bucket}. Set the RPO, pick a moment on the timeline, choose a target, and restore.</p>
        </div>
        <div className="space-y-6 p-5">
        <div className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Recovery point objective</h3>
            <Badge tone="green">Verified {selected.verifiedAt}</Badge>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {RPO_OPTIONS.map((o) => (
              <button
                key={o.value}
                onClick={() => setRpo(o)}
                className={`rounded-full px-4 py-2 text-sm font-medium transition ${rpo.value === o.value ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'}`}
              >
                {o.label}
              </button>
            ))}
          </div>
          <p className="mt-4 text-sm text-slate-600">{rpo.note}</p>
          <div className="mt-3 grid grid-cols-3 gap-3 text-center text-xs sm:text-sm">
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Log shipping</p><p className="mt-1 font-semibold text-slate-900">{rpo.binlog}</p></div>
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Full backup</p><p className="mt-1 font-semibold text-slate-900">{rpo.full}</p></div>
            <div className="rounded-lg bg-slate-50 p-3"><p className="text-slate-500">Worst case loss</p><p className="mt-1 font-semibold text-slate-900">{rpo.worst}</p></div>
          </div>
        </div>

        <div className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Point-in-time restore</h3>
          <p className="mt-2 text-sm text-slate-600">
            Pick a point on the timeline. The restore loads the 01:00 full backup and replays transaction logs up to that moment.
          </p>
          <p className="mt-2 text-xs font-medium text-indigo-700">
            Timeline step: {RPO_STEP_LABEL[rpo.value]}, matches your RPO. Change the RPO above and the timeline ticks change with it.
          </p>
          <div className="mt-4">
            <input
              type="range"
              min="0"
              max={timelinePoints.length - 1}
              value={point}
              onChange={(e) => setPoint(Number(e.target.value))}
              className="w-full"
              aria-label="Restore point in time"
            />
            <div className="mt-1 flex justify-between text-xs text-slate-500">
              {timelineLabels.map((p) => (
                <button key={`${p.label}-${p.index}`} onClick={() => setPoint(p.index)} className={point === p.index ? 'font-semibold text-indigo-600' : ''}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
          <div className="mt-4 rounded-lg bg-indigo-50 p-3 text-sm text-indigo-900">
            Restore to <strong>{chosen.time}</strong>: {chosen.desc}
          </div>
          <div className="mt-4">
            <label className="text-sm font-medium text-slate-700">Restore target server</label>
            <div className="mt-2 grid gap-2 sm:grid-cols-3">
              {TARGETS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTarget(t)}
                  className={`rounded-lg border p-3 text-left text-sm transition ${target.id === t.id ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500' : 'border-slate-200 hover:border-slate-300'}`}
                >
                  <span className="font-mono font-semibold text-slate-900">{t.name}</span>
                  <span className="mt-1 block text-xs text-slate-500">{t.detail}</span>
                  <span className="block font-mono text-xs text-slate-500">{t.host}</span>
                </button>
              ))}
            </div>
          </div>
          <button
            onClick={runRestore}
            disabled={restoring}
            className="mt-4 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-50"
          >
            {restoring ? 'Restoring...' : `Restore ${selected.name} to ${chosen.time} on ${target.name}`}
          </button>
        </div>

        <div className="overflow-hidden rounded-xl bg-slate-900 shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-700 px-4 py-2">
            <span className="font-mono text-xs font-semibold uppercase tracking-wide text-slate-300">Restore log</span>
            <span className="font-mono text-xs text-emerald-400">live demo log</span>
          </div>
          <div className="log-scroll max-h-56 overflow-y-auto p-4 font-mono text-xs leading-relaxed text-slate-200">
            {restoreLog.map((line, i) => (
              <p key={i}>{line}</p>
            ))}
          </div>
        </div>
        </div>
      </section>
    </div>
    </div>
  )
}

const ENGINES = [
  { name: 'MySQL', phase: 'Phase 1', detail: 'mysqldump plus binlog point-in-time restore. The working restore flow exists today.', status: 'Live in the lab', tone: 'green' },
  { name: 'MariaDB', phase: 'Phase 1', detail: 'Same binlog flow as MySQL. Covered by the same backup and restore path.', status: 'Live in the lab', tone: 'green' },
  { name: 'PostgreSQL', phase: 'Phase 1', detail: 'pg_dump plus WAL point-in-time restore. Same dashboard, same proof runs.', status: 'In build', tone: 'indigo' },
  { name: 'MongoDB', phase: 'Phase 2', detail: 'mongodump plus oplog point-in-time restore. Next engine after PostgreSQL.', status: 'Phase 2', tone: 'amber' },
  { name: 'SQL Server', phase: 'On demand', detail: 'Added when a client asks for it. Same verification model.', status: 'On demand', tone: 'slate' },
  { name: 'Oracle', phase: 'On demand', detail: 'Added when a client asks for it. Same verification model.', status: 'On demand', tone: 'slate' },
]

const PROOF_STEPS = [
  { n: '1', title: 'Take the backup', text: 'Full backup on schedule, transaction logs shipped on your RPO interval, all to your own bucket (MinIO or S3).' },
  { n: '2', title: 'Restore it for real', text: 'On a schedule you set, the product restores the latest backup to a target server you registered, not a throwaway container.' },
  { n: '3', title: 'Replay the logs', text: 'Transaction logs replay up to the proof point, so the restored copy matches the source at that moment.' },
  { n: '4', title: 'Check the data', text: 'Databases, tables and row counts are compared against the source. A mismatch fails the proof, loudly.' },
  { n: '5', title: 'Stamp the proof', text: 'A dated, auditable verified stamp: which backup, which target, how long the restore took, and what was checked.' },
]

const FLOW_STEPS = [
  { n: '1', title: 'Pick a database', text: 'MySQL, PostgreSQL or MongoDB. Choose the source you want to protect.' },
  { n: '2', title: 'Connect it to us', text: 'Add the connection details once. We reach it over your network, nothing to install on the database.' },
  { n: '3', title: 'Pick the bucket', text: 'MinIO in your data centre, Amazon S3, or any S3-compatible store. Backups live away from the source.' },
  { n: '4', title: 'Set the RPO', text: '5 minutes, 15 minutes, 1 hour or 24 hours. The promise decides how often logs ship.' },
  { n: '5', title: 'We prove it works', text: 'On a schedule, we restore the latest backup to a real target and check the data. You get a dated verified stamp.' },
  { n: '6', title: 'Restore any moment', text: 'At disaster time, pick a point on the timeline and a target server, and restore to that exact moment.' },
]

function UserFlow() {
  const [active, setActive] = useState(0)
  const [cycle, setCycle] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setActive((a) => (a + 1) % FLOW_STEPS.length)
    }, 1800)
    return () => clearInterval(timer)
  }, [cycle])

  function replay() {
    setActive(0)
    setCycle((c) => c + 1)
  }

  const pct = (active / (FLOW_STEPS.length - 1)) * 100

  return (
    <div>
      <div className="relative mb-6 hidden h-1 rounded-full bg-slate-200 md:block">
        <div className="h-full rounded-full bg-indigo-600 transition-all duration-700" style={{ width: `${pct}%` }} />
        <span className="absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full bg-indigo-600 ring-4 ring-indigo-100 transition-all duration-700" style={{ left: `${pct}%` }} />
      </div>
      <div className="grid gap-4 md:grid-cols-3 lg:grid-cols-6">
        {FLOW_STEPS.map((s, i) => {
          const state = i < active ? 'done' : i === active ? 'active' : 'todo'
          return (
            <button
              key={s.n}
              onClick={() => setActive(i)}
              className={`rounded-xl p-4 text-left shadow-sm ring-1 transition ${state === 'active' ? 'bg-indigo-50 ring-2 ring-indigo-600' : state === 'done' ? 'bg-white ring-emerald-300' : 'bg-white ring-slate-200'}`}
            >
              <span className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-bold text-white ${state === 'done' ? 'bg-emerald-600' : state === 'active' ? 'bg-indigo-600' : 'bg-slate-400'}`}>
                {state === 'done' ? '✓' : s.n}
              </span>
              <h3 className="mt-3 text-sm font-semibold text-slate-900">{s.title}</h3>
              <p className="mt-1 text-xs leading-relaxed text-slate-600">{s.text}</p>
            </button>
          )
        })}
      </div>
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button onClick={replay} className="rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700">
          Replay the flow
        </button>
        <p className="text-sm text-slate-600">
          Now: step {active + 1} of {FLOW_STEPS.length}, {FLOW_STEPS[active].title}. Tap any step to jump to it.
        </p>
      </div>
    </div>
  )
}

function goDashboard() {
  window.location.hash = '#/dashboard'
}

function goHome() {
  window.location.hash = '#/'
}

function HomeHeader() {
  const [nav, setNav] = useState('top')
  function go(id) {
    setNav(id)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
  }

  return (
    <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6">
        <button onClick={() => go('top')} className="flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-600 font-mono text-sm font-bold text-white">VR</span>
          <span className="text-left leading-tight">
            <span className="block text-base font-bold text-slate-900">Verified Restore</span>
            <span className="block text-xs text-slate-500">Proven recovery for self-managed databases</span>
          </span>
        </button>
        <nav className="hidden items-center gap-1 text-sm font-medium text-slate-600 md:flex">
          <button onClick={() => go('flow')} className={`rounded-lg px-3 py-2 hover:bg-slate-100 ${nav === 'flow' ? 'text-indigo-600' : ''}`}>User flow</button>
          <button onClick={() => go('engines')} className={`rounded-lg px-3 py-2 hover:bg-slate-100 ${nav === 'engines' ? 'text-indigo-600' : ''}`}>Engines</button>
          <button onClick={() => go('proof')} className={`rounded-lg px-3 py-2 hover:bg-slate-100 ${nav === 'proof' ? 'text-indigo-600' : ''}`}>How proof works</button>
          <button onClick={goDashboard} className="rounded-lg px-3 py-2 hover:bg-slate-100">Dashboard</button>
          <button onClick={goDashboard} className="ml-2 rounded-lg bg-indigo-600 px-4 py-2 text-white hover:bg-indigo-700">Try the demo</button>
        </nav>
      </div>
    </header>
  )
}

function HomeView() {
  return (
    <div className="min-h-screen">
      <HomeHeader />

      <main id="top">
        <section className="bg-slate-900 text-white">
          <div className="mx-auto max-w-7xl px-4 py-16 sm:px-6 sm:py-24">
            <Badge tone="indigo">Product mock: sample data, no real connections</Badge>
            <h1 className="mt-5 max-w-4xl text-4xl font-bold leading-tight sm:text-5xl">
              Proven recovery for the databases you run yourself.
            </h1>
            <p className="mt-5 max-w-3xl text-lg text-slate-300">
              We protect MySQL, MariaDB, PostgreSQL and MongoDB running in your data centre or on your own servers, prove every backup by actually restoring it, and recover your data to any point in time, on a server you choose.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <button onClick={goDashboard} className="rounded-lg bg-indigo-600 px-5 py-3 text-sm font-semibold text-white hover:bg-indigo-500">See the dashboard</button>
              <button onClick={() => document.getElementById('proof')?.scrollIntoView({ behavior: 'smooth' })} className="rounded-lg bg-white/10 px-5 py-3 text-sm font-semibold text-white ring-1 ring-white/25 hover:bg-white/20">How proof works</button>
            </div>
            <div className="mt-12 grid gap-4 md:grid-cols-3">
              <div className="rounded-xl bg-white/5 p-5 ring-1 ring-white/10">
                <p className="font-mono text-xs font-semibold uppercase tracking-wide text-indigo-300">1. You set the promise</p>
                <p className="mt-2 text-sm text-slate-300">Pick your RPO: 5 minutes, 15 minutes, 1 hour or 24 hours. That promise decides how often logs ship and backups run.</p>
              </div>
              <div className="rounded-xl bg-white/5 p-5 ring-1 ring-white/10">
                <p className="font-mono text-xs font-semibold uppercase tracking-wide text-indigo-300">2. We prove it works</p>
                <p className="mt-2 text-sm text-slate-300">On a schedule, we actually restore your latest backup to a real target server and check the data. You get a dated, auditable verified stamp.</p>
              </div>
              <div className="rounded-xl bg-white/5 p-5 ring-1 ring-white/10">
                <p className="font-mono text-xs font-semibold uppercase tracking-wide text-indigo-300">3. You recover fast</p>
                <p className="mt-2 text-sm text-slate-300">At disaster time, pick a backup, pick a moment on the timeline, pick a registered server, and restore. No scrambling, no guessing.</p>
              </div>
            </div>
          </div>
        </section>

        <section id="flow" className="border-t border-slate-200 bg-white">
          <div className="mx-auto max-w-7xl scroll-mt-20 px-4 py-14 sm:px-6">
            <SectionHeading
              kicker="User journey"
              title="How a user flows through it"
              sub="Six steps, in order. Watch the flow travel from left to right, or replay it. Tap any step to jump to it."
            />
            <UserFlow />
          </div>
        </section>

        <section className="border-t border-slate-200 bg-indigo-50">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-6 px-4 py-14 sm:px-6">
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-indigo-600">Try it yourself</p>
              <h2 className="mt-1 text-2xl font-bold text-slate-900 sm:text-3xl">See the dashboard in action</h2>
              <p className="mt-2 max-w-2xl text-slate-600">Open the full dashboard on its own page. Run a backup, watch it flow to the bucket, pick an RPO, move the timeline, and restore to any moment.</p>
            </div>
            <button onClick={goDashboard} className="rounded-lg bg-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700">Open the dashboard</button>
          </div>
        </section>

        <section id="engines" className="border-t border-slate-200 bg-white">
          <div className="mx-auto max-w-7xl scroll-mt-20 px-4 py-14 sm:px-6">
            <SectionHeading
              kicker="Engines covered"
              title="One dashboard, every database you run"
              sub="Phase 1 covers the engines with a working restore flow today. Later engines follow the same verification model, added on client demand."
            />
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {ENGINES.map((e) => (
                <div key={e.name} className="rounded-xl p-5 shadow-sm ring-1 ring-slate-200">
                  <div className="flex items-center justify-between">
                    <h3 className="text-lg font-bold text-slate-900">{e.name}</h3>
                    <Badge tone={e.tone}>{e.phase}</Badge>
                  </div>
                  <p className="mt-2 text-sm text-slate-600">{e.detail}</p>
                  <p className="mt-3 text-xs font-medium text-slate-500">Status: {e.status}</p>
                </div>
              ))}
            </div>
            <p className="mt-6 max-w-3xl text-sm text-slate-500">
              Honest limit: logical dumps are slow past about 50 GB. For large databases, physical backup (for example XtraBackup for MySQL) is the right path, and it fits the same proof flow.
            </p>
          </div>
        </section>

        <section id="proof" className="border-t border-slate-200">
          <div className="mx-auto max-w-7xl scroll-mt-20 px-4 py-14 sm:px-6">
            <SectionHeading
              kicker="How verification works"
              title="A backup nobody has restored is just a hope"
              sub="Most tools take backups and stop. Verified Restore keeps going: it restores the backup, checks the data, and stamps the proof. That stamp is what you show an auditor."
            />
            <div className="grid gap-4 md:grid-cols-5">
              {PROOF_STEPS.map((s) => (
                <div key={s.n} className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-indigo-600 text-sm font-bold text-white">{s.n}</span>
                  <h3 className="mt-3 font-semibold text-slate-900">{s.title}</h3>
                  <p className="mt-1 text-sm text-slate-600">{s.text}</p>
                </div>
              ))}
            </div>
            <div className="mt-8 rounded-xl bg-slate-900 p-6 text-white">
              <p className="font-mono text-xs font-semibold uppercase tracking-wide text-indigo-300">Why this is different</p>
              <p className="mt-2 max-w-3xl text-slate-300">
                Verification-only tools cover one engine. Free backup dashboards verify MySQL only, with logical dumps and no point-in-time restore. No competitor combines multi-engine backup, point-in-time restore to any second, restore to a server you choose, and a self-hosted dashboard in one product.
              </p>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-6 text-sm text-slate-500 sm:px-6">
          <p><strong className="text-slate-800">Verified Restore</strong> | Product mock for demonstration. All data shown is sample data.</p>
          <p>Built by the DevOps team, OpsTree Global.</p>
        </div>
      </footer>
    </div>
  )
}

function DashboardView({ context, onContext }) {
  useEffect(() => {
    window.scrollTo(0, 0)
  }, [])

  return (
    <div className="min-h-screen bg-slate-100">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
          <button onClick={goHome} className="flex items-center gap-2">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-600 font-mono text-sm font-bold text-white">VR</span>
            <span className="text-left leading-tight">
              <span className="block text-base font-bold text-slate-900">Verified Restore</span>
              <span className="block text-xs text-slate-500">Interactive demo dashboard</span>
            </span>
          </button>
          {context && (
            <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-full bg-indigo-50 px-3 py-1 font-medium text-indigo-700">Source: {context.source}</span>
              <span className="rounded-full bg-slate-100 px-3 py-1 font-medium text-slate-700">RPO: {context.rpo}</span>
              <span className="rounded-full bg-slate-100 px-3 py-1 font-medium text-slate-700">Bucket: {context.bucket}</span>
            </div>
          )}
          <button onClick={goHome} className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700">Back to home</button>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <div className="mb-6">
          <p className="text-sm font-semibold uppercase tracking-wide text-indigo-600">Interactive demo</p>
          <h1 className="mt-1 text-2xl font-bold text-slate-900 sm:text-3xl">The dashboard</h1>
          <p className="mt-2 max-w-3xl text-slate-600">Sample data, no real connections. Left side backs up: pick a source, choose the bucket, and run a backup. Right side restores: set the RPO, move the timeline, choose a target, and run a restore.</p>
        </div>
        <Dashboard onContext={onContext} />
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-5 text-sm text-slate-500 sm:px-6">
          <p><strong className="text-slate-800">Verified Restore</strong> | Product mock for demonstration. All data shown is sample data.</p>
          <button onClick={goHome} className="font-medium text-indigo-600 hover:text-indigo-800">Back to home</button>
        </div>
      </footer>
    </div>
  )
}

function readRoute() {
  const hash = window.location.hash.replace(/^#\/?/, '')
  return hash === 'dashboard' ? 'dashboard' : 'home'
}

export default function App() {
  const [route, setRoute] = useState(() => readRoute())
  const [dashContext, setDashContext] = useState(null)

  useEffect(() => {
    const onHash = () => {
      setRoute(readRoute())
      window.scrollTo(0, 0)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  if (route === 'dashboard') {
    return <DashboardView context={dashContext} onContext={setDashContext} />
  }
  return <HomeView />
}
