import React, { useEffect, useRef, useState } from 'react'
import { api, getUser as getMe } from '../api.js'
import { Confirm, Icon, toast } from '../ui.jsx'
import './token-dashboard.css'

const me = () => getMe()

const money = (v) => (v == null ? '—' : '$' + Number(v).toFixed(2))

function Stat({ label, value, accent }) {
  return (
    <div className={'tkd-stat' + (accent ? ' is-accent' : '')}>
      <span className="tkd-stat-label">{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export default function TokenDashboard() {
  const [data, setData] = useState(null)
  const [allRows, setAllRows] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [plaintext, setPlaintext] = useState(null)
  const [confirmAction, setConfirmAction] = useState(null)
  const req = useRef(0)

  const load = (force = false) => {
    const current = ++req.current
    setLoading(true)
    setError('')
    const jobs = [api.nekoworkUsageMe(force)]
    if (me && me.isSuper) jobs.push(api.nekoworkUsageAll(force))
    Promise.all(jobs).then(([mine, all]) => {
      if (current !== req.current) return
      setData(mine)
      if (me && me.isSuper) setAllRows((all && all.items) || [])
    }).catch((reason) => {
      if (current === req.current) setError(reason.message || 'Failed to load usage')
    }).finally(() => {
      if (current === req.current) setLoading(false)
    })
  }

  useEffect(() => { load(false) }, [])

  const runTokenAction = (action) => {
    setBusy(action)
    const call = action === 'generate' ? api.nekoworkGenerateToken
      : action === 'reset' ? api.nekoworkResetToken
        : api.nekoworkRevokeToken
    call().then((result) => {
      if (action === 'revoke') {
        toast('Token revoked')
        setPlaintext(null)
      } else {
        setPlaintext(result)
      }
      load(true)
    }).catch((reason) => {
      toast(reason.message || 'Action failed')
    }).finally(() => {
      setBusy('')
      setConfirmAction(null)
    })
  }

  const token = data && data.token
  const usage = (data && data.usage) || { days: [], today_usd: 0, month_usd: 0, window_usd: 0 }
  const days = usage.days || []
  const maxUsd = Math.max(0.01, ...days.map((d) => d.usd))
  const hasToken = token && token.has_token

  return (
    <div className="page tkd-page">
      <div className="tkd-head">
        <div>
          <h2>Token LLM</h2>
          <p className="tkd-sub">New API gateway token — saldo dan pemakaian pribadi.</p>
        </div>
        <div className="tkd-actions">
          <button className="ghost" onClick={() => load(true)} disabled={loading}>
            <Icon name="pulse" /> Refresh
          </button>
          {!hasToken && (
            <button onClick={() => runTokenAction('generate')} disabled={busy === 'generate'}>
              {busy === 'generate' ? 'Generating…' : 'Generate token'}
            </button>
          )}
          {hasToken && (
            <>
              <button className="ghost" onClick={() => setConfirmAction('reset')} disabled={busy}>Reset</button>
              <button className="danger" onClick={() => setConfirmAction('revoke')} disabled={busy}>Revoke</button>
            </>
          )}
        </div>
      </div>

      {error && <div className="tkd-error">{error}</div>}

      {plaintext && (
        <div className="tkd-key">
          <div className="tkd-key-warn">Copy sekarang — key cuma ditampilkan sekali.</div>
          <code>{plaintext.token}</code>
          <button onClick={() => { navigator.clipboard.writeText(plaintext.token); toast('Copied') }}>Copy</button>
        </div>
      )}

      <div className="tkd-status">
        <span className={'tkd-dot ' + (hasToken && token.token_enabled ? 'ok' : 'off')} />
        <span>{!hasToken ? 'Belum ada token' : token.token_enabled ? 'Aktif' : 'Disabled'}</span>
        <code className="tkd-masked">{token && token.masked_key ? token.masked_key : ''}</code>
      </div>

      <div className="tkd-stats">
        <Stat label="Saldo" accent value={token && token.unlimited ? 'Unlimited' : money(token && token.balance_usd)} />
        <Stat label="Pakai hari ini" value={money(usage.today_usd)} />
        <Stat label="Pakai bulan ini" value={money(usage.month_usd)} />
        <Stat label="14 hari terakhir" value={money(usage.window_usd)} />
      </div>

      <div className="tkd-chart">
        <div className="tkd-chart-title">Riwayat 14 hari</div>
        <div className="tkd-bars">
          {days.map((d) => (
            <div key={d.date} className="tkd-bar-col" title={`${d.date} — ${money(d.usd)} (${d.requests} req)`}>
              <div className="tkd-bar" style={{ height: `${Math.round((d.usd / maxUsd) * 100)}%` }} />
              <span className="tkd-bar-day">{d.date.slice(8)}</span>
            </div>
          ))}
        </div>
      </div>

      {me && me.isSuper && (
        <div className="tkd-admin">
          <div className="tkd-chart-title">Semua user</div>
          <table className="tkd-table">
            <thead>
              <tr><th>User</th><th>Status</th><th>Saldo</th><th>Hari ini</th><th>Bulan ini</th></tr>
            </thead>
            <tbody>
              {(allRows || []).map((r) => (
                <tr key={r.user_id}>
                  <td>{r.display_name || r.email}</td>
                  <td><span className={'tkd-dot ' + (r.token_enabled ? 'ok' : 'off')} /></td>
                  <td>{r.balance_usd == null ? '—' : money(r.balance_usd)}</td>
                  <td>{money(r.usage && r.usage.today_usd)}</td>
                  <td>{money(r.usage && r.usage.month_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {confirmAction === 'reset' && (
        <Confirm
          title="Reset token?"
          text="Token lama langsung tidak valid. Key baru ditampilkan sekali."
          danger
          confirmText="Reset"
          onConfirm={() => runTokenAction('reset')}
          onClose={() => setConfirmAction(null)}
        />
      )}
      {confirmAction === 'revoke' && (
        <Confirm
          title="Revoke token?"
          text="Token dihapus dari gateway. Yang pakai token ini akan gagal auth."
          danger
          confirmText="Revoke"
          onConfirm={() => runTokenAction('revoke')}
          onClose={() => setConfirmAction(null)}
        />
      )}
    </div>
  )
}
