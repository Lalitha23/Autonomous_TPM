// Panel 2 — Backlog View
// Ticket table with filter buttons (visual only, no filtering logic as per spec)
// Matches atis_dashboard_v3.html Panel 2 exactly

import { useState } from 'react'

const FLAG_STYLE = {
  BLOCKED:    { background: '#fef2f2', color: '#dc2626' },
  STALE:      { background: '#eff6ff', color: '#1d4ed8' },
  OVERLOADED: { background: '#fffbeb', color: '#d97706' },
  SCOPE_CREEP:{ background: '#f0fdf4', color: '#16a34a' },
}

const FLAG_LABEL = {
  BLOCKED:     'Blocked',
  STALE:       'Stale',
  OVERLOADED:  'Overload',
  SCOPE_CREEP: 'Scope',
}

const S = {
  heading: {
    fontSize: '22px', fontWeight: 700, fontStyle: 'italic',
    color: '#0f172a', marginBottom: '6px', fontFamily: 'Georgia, serif',
  },
  sub: {
    fontSize: '13px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif', marginBottom: '28px',
  },
  filterRow: { display: 'flex', gap: '8px', marginBottom: '20px', flexWrap: 'wrap' },
  btn: (active) => ({
    fontSize: '11px', padding: '5px 14px', borderRadius: '4px',
    border: active ? 'none' : '0.5px solid #d6d3d1',
    background: active ? '#0f172a' : '#ffffff',
    color: active ? '#ffffff' : '#44403c',
    cursor: 'pointer', fontFamily: 'system-ui, sans-serif',
  }),
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    fontSize: '9px', fontWeight: 700, letterSpacing: '0.1em',
    textTransform: 'uppercase', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
    padding: '0 12px 10px 0', textAlign: 'left',
    borderBottom: '1.5px solid #0f172a',
  },
  td: {
    padding: '13px 12px 13px 0', borderBottom: '0.5px solid #e7e5e4',
    verticalAlign: 'middle',
  },
  tdLast: {
    padding: '13px 12px 13px 0', verticalAlign: 'middle',
  },
  ticketId: {
    fontSize: '12px', color: '#44403c',
    fontFamily: 'Georgia, serif', fontStyle: 'italic', whiteSpace: 'nowrap',
  },
  ticketTitle: { fontSize: '13px', color: '#1c1917' },
  ticketTeam: {
    fontSize: '11px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
    textTransform: 'uppercase', letterSpacing: '0.04em', whiteSpace: 'nowrap',
  },
  ticketStatus: {
    fontSize: '11px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
  },
  flag: {
    fontSize: '9px', fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.06em', padding: '3px 9px', borderRadius: '3px',
    whiteSpace: 'nowrap', fontFamily: 'system-ui, sans-serif',
  },
  noFlag: {
    color: '#a8a29e', fontSize: '11px',
    fontFamily: 'system-ui, sans-serif',
  },
  empty: {
    fontSize: '13px', color: '#78716c',
    fontFamily: 'system-ui, sans-serif',
    padding: '40px 0', textAlign: 'center',
  },
}

const STATUS_LABEL = {
  TODO:      'Todo',
  IN_PROGRESS: 'In Progress',
  IN_REVIEW: 'In Review',
  BLOCKED:   'Blocked',
  DONE:      'Done',
}

const FLAG_FILTERS = ['All', 'Blocked', 'Stale', 'Overload', 'Scope']
const TEAM_FILTERS = ['Platform', 'Payments', 'Mobile']

export default function BacklogView({ tickets }) {
  const [activeFlag, setActiveFlag] = useState('All')
  const [activeTeam, setActiveTeam] = useState(null)

  // Filter logic (flag filter maps display name → risk_flag value)
  const FLAG_MAP = { Blocked: 'BLOCKED', Stale: 'STALE', Overload: 'OVERLOADED', Scope: 'SCOPE_CREEP' }
  const filtered = tickets.filter(t => {
    const flagMatch = activeFlag === 'All' || t.risk_flag === FLAG_MAP[activeFlag]
    const teamMatch = !activeTeam || t.team === activeTeam
    return flagMatch && teamMatch
  })

  // Sort: flagged first, then by priority
  const PRI_ORDER = { P0: 0, P1: 1, P2: 2, P3: 3 }
  const sorted = [...filtered].sort((a, b) => {
    const aF = a.risk_flag ? 0 : 1
    const bF = b.risk_flag ? 0 : 1
    if (aF !== bF) return aF - bF
    return (PRI_ORDER[a.priority] ?? 9) - (PRI_ORDER[b.priority] ?? 9)
  })

  return (
    <div>
      <div style={S.heading}>Backlog View</div>
      <div style={S.sub}>All tickets with agent-injected risk flags. Filter by type or team.</div>

      <div style={S.filterRow}>
        {FLAG_FILTERS.map(f => (
          <button
            key={f}
            style={S.btn(activeFlag === f)}
            onClick={() => setActiveFlag(f)}
          >
            {f}
          </button>
        ))}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
          {TEAM_FILTERS.map(t => (
            <button
              key={t}
              style={S.btn(activeTeam === t)}
              onClick={() => setActiveTeam(prev => prev === t ? null : t)}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {sorted.length === 0 ? (
        <div style={S.empty}>No tickets match the current filter.</div>
      ) : (
        <table style={S.table}>
          <thead>
            <tr>
              <th style={S.th}>ID</th>
              <th style={S.th}>Title</th>
              <th style={S.th}>Team</th>
              <th style={S.th}>Status</th>
              <th style={S.th}>Flag</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((t, i) => {
              const isLast = i === sorted.length - 1
              const tdStyle = isLast ? S.tdLast : S.td
              const flagStyle = FLAG_STYLE[t.risk_flag]
              return (
                <tr key={t.id}>
                  <td style={tdStyle}>
                    <span style={S.ticketId}>{t.id}</span>
                  </td>
                  <td style={tdStyle}>
                    <span style={S.ticketTitle}>{t.title}</span>
                  </td>
                  <td style={tdStyle}>
                    <span style={S.ticketTeam}>{t.team}</span>
                  </td>
                  <td style={tdStyle}>
                    <span style={S.ticketStatus}>{STATUS_LABEL[t.status] || t.status}</span>
                  </td>
                  <td style={tdStyle}>
                    {flagStyle ? (
                      <span style={{ ...S.flag, ...flagStyle }}>
                        {FLAG_LABEL[t.risk_flag] || t.risk_flag}
                      </span>
                    ) : (
                      <span style={S.noFlag}>—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
