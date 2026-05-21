// Panel 3 — Agent Activity Stream
// Cards with collapsible reasoning traces
// Matches atis_dashboard_v3.html Panel 3 exactly

import { useState } from 'react'

const AGENT_TAG = {
  telemetry:     { background: '#f1f5f9', color: '#1e293b', border: '0.5px solid #cbd5e1' },
  risk_detection:{ background: '#fef2f2', color: '#dc2626', border: '0.5px solid #fecaca' },
  dependency_analysis: { background: '#eff6ff', color: '#1d4ed8', border: '0.5px solid #bfdbfe' },
  mitigation:    { background: '#fffbeb', color: '#b45309', border: '0.5px solid #fde68a' },
  communication: { background: '#f0fdf4', color: '#15803d', border: '0.5px solid #bbf7d0' },
}

const AGENT_LABEL = {
  telemetry:           'Telemetry',
  risk_detection:      'Risk Detection',
  dependency_analysis: 'Dependency',
  mitigation:          'Mitigation',
  communication:       'Communication',
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
  card: {
    background: '#ffffff', border: '0.5px solid #d6d3d1',
    borderRadius: '8px', padding: '20px 24px', marginBottom: '14px',
  },
  header: { display: 'flex', alignItems: 'flex-start', gap: '12px' },
  tag: {
    fontSize: '9px', fontWeight: 700, letterSpacing: '0.08em',
    textTransform: 'uppercase', padding: '3px 9px', borderRadius: '3px',
    flexShrink: 0, marginTop: '2px', fontFamily: 'system-ui, sans-serif',
  },
  body: { flex: 1 },
  decision: {
    fontSize: '13px', color: '#0f172a',
    fontFamily: 'system-ui, sans-serif', fontWeight: 500, lineHeight: 1.5,
  },
  expand: {
    fontSize: '11px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
    marginTop: '7px', cursor: 'pointer',
    display: 'inline-flex', alignItems: 'center', gap: '4px',
    userSelect: 'none',
  },
  reasoning: (open) => ({
    fontSize: '12px', color: '#44403c', fontStyle: 'italic',
    marginTop: '10px', padding: '10px 14px', background: '#fafaf9',
    borderLeft: '2px solid #d6d3d1', borderRadius: '0 4px 4px 0',
    lineHeight: 1.65,
    display: open ? 'block' : 'none',
  }),
  time: {
    fontSize: '11px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
    flexShrink: 0, marginTop: '2px',
  },
  empty: {
    fontSize: '13px', color: '#78716c',
    fontFamily: 'system-ui, sans-serif',
    padding: '40px 0', textAlign: 'center',
  },
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  try {
    return new Date(isoStr).toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    })
  } catch {
    return ''
  }
}

function ActivityCard({ decision }) {
  const [open, setOpen] = useState(false)
  const agentKey = decision.agent_name || decision.agent || 'unknown'
  const tagStyle = AGENT_TAG[agentKey] || { background: '#e7e5e4', color: '#44403c', border: '0.5px solid #d6d3d1' }
  const label = AGENT_LABEL[agentKey] || agentKey

  const hasReasoning = Boolean(decision.reasoning && decision.reasoning.trim())
  const timestamp = formatTime(decision.timestamp || decision.created_at)

  return (
    <div style={S.card}>
      <div style={S.header}>
        <span style={{ ...S.tag, ...tagStyle }}>{label}</span>
        <div style={S.body}>
          <div style={S.decision}>{decision.decision}</div>
          {hasReasoning && (
            <>
              <div
                style={S.expand}
                onClick={() => setOpen(o => !o)}
                onMouseEnter={e => e.currentTarget.style.color = '#0f172a'}
                onMouseLeave={e => e.currentTarget.style.color = '#44403c'}
              >
                <svg
                  width="12" height="12" viewBox="0 0 12 12"
                  fill="none" stroke="currentColor" strokeWidth="1.5"
                  className={`atis-chevron${open ? ' atis-chevron-open' : ''}`}
                  style={{ flexShrink: 0, transition: 'transform 0.2s' }}
                >
                  <polyline points="2,4 6,8 10,4"/>
                </svg>
                {open ? ' Collapse reasoning' : ' Click to expand reasoning'}
              </div>
              <div style={S.reasoning(open)}>
                {decision.reasoning}
              </div>
            </>
          )}
        </div>
        {timestamp && <span style={S.time}>{timestamp}</span>}
      </div>
    </div>
  )
}

export default function AgentActivity({ decisions }) {
  if (!decisions || decisions.length === 0) {
    return (
      <div>
        <div style={S.heading}>Agent Activity Stream</div>
        <div style={S.sub}>Every decision made by each agent this cycle, with full reasoning traces.</div>
        <div style={S.empty}>
          Waiting for the first agent cycle to complete…
          <br />
          <span style={{ fontSize: '11px', marginTop: '8px', display: 'block' }}>
            Cycles run every 30 seconds.
          </span>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div style={S.heading}>Agent Activity Stream</div>
      <div style={S.sub}>Every decision made by each agent this cycle, with full reasoning traces.</div>
      {decisions.map((d, i) => (
        <ActivityCard key={`${d.run_id || ''}-${d.agent_name || d.agent}-${i}`} decision={d} />
      ))}
    </div>
  )
}
