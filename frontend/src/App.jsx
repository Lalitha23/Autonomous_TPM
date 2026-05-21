import { useEffect, useRef, useState } from 'react'
import Topbar from './components/Topbar'
import SummaryBar from './components/SummaryBar'
import Sidebar from './components/Sidebar'
import ProgramHealth from './components/panels/ProgramHealth'
import BacklogView from './components/panels/BacklogView'
import AgentActivity from './components/panels/AgentActivity'
import ExecutiveOutputs from './components/panels/ExecutiveOutputs'

const PROGRAM_ID = null // fetched from /api/programs on mount
const WS_RECONNECT_DELAY = 3000

export default function App() {
  const [programId, setProgramId] = useState(null)
  const [program, setProgram]     = useState(null)
  const [sprints, setSprints]     = useState([])
  const [tickets, setTickets]     = useState([])
  const [outputs, setOutputs]     = useState({
    standup_summary: null,
    escalation_memo: null,
    risk_digest: null,
  })
  const [decisions, setDecisions] = useState([])
  const [activePanel, setActivePanel] = useState('health')
  const [wsStatus, setWsStatus]   = useState('connecting') // connecting | live | disconnected
  const [lastCycle, setLastCycle] = useState(null) // {run_id, cycle_number, completed_at}

  const wsRef       = useRef(null)
  const lastRunIdRef = useRef(null)
  const reconnectRef = useRef(null)

  // ── Load program on mount ─────────────────────────────────────────────
  useEffect(() => {
    fetch('/api/programs')
      .then(r => r.json())
      .then(programs => {
        if (programs.length === 0) return
        const p = programs[0]
        setProgramId(p.id)
        setProgram(p)
      })
      .catch(err => console.error('Failed to fetch programs:', err))
  }, [])

  // ── WebSocket connection ───────────────────────────────────────────────
  useEffect(() => {
    if (!programId) return

    function connect() {
      const lastRunId = lastRunIdRef.current
      const qs = lastRunId ? `?last_run_id=${lastRunId}` : ''
      const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${programId}${qs}`

      const ws = new WebSocket(wsUrl)
      wsRef.current = ws
      setWsStatus('connecting')

      ws.onopen = () => {
        console.log('[ATIS] WebSocket connected')
        setWsStatus('live')
        if (reconnectRef.current) {
          clearTimeout(reconnectRef.current)
          reconnectRef.current = null
        }
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)

          if (msg.type === 'init') {
            const { sprints: s, tickets: t, outputs: o } = msg.data
            if (s) setSprints(s)
            if (t) setTickets(t)
            if (o) setOutputs(o)
          }

          else if (msg.type === 'cycle_complete') {
            const { run_id, cycle_number, sprints: s, tickets: t, outputs: o, decisions: d } = msg.data
            if (s && s.length > 0) setSprints(s)
            if (t && t.length > 0) setTickets(t)
            if (o) setOutputs(prev => ({ ...prev, ...o }))
            if (d && d.length > 0) {
              setDecisions(prev => [...d, ...prev].slice(0, 200))
            }
            lastRunIdRef.current = run_id
            setLastCycle({ run_id, cycle_number, completed_at: msg.data.completed_at })
          }

          else if (msg.type === 'agent_decision') {
            setDecisions(prev => [msg.data, ...prev].slice(0, 200))
          }
        } catch (err) {
          console.error('[ATIS] WS parse error:', err)
        }
      }

      ws.onerror = (err) => {
        console.error('[ATIS] WebSocket error:', err)
      }

      ws.onclose = (event) => {
        console.warn('[ATIS] WebSocket closed, code=', event.code)
        setWsStatus('disconnected')
        // Reconnect after 3 seconds
        reconnectRef.current = setTimeout(() => {
          if (wsRef.current === ws) { // only reconnect if this is still the active ws
            connect()
          }
        }, WS_RECONNECT_DELAY)
      }
    }

    connect()

    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      if (wsRef.current) {
        wsRef.current.onclose = null // prevent reconnect loop on unmount
        wsRef.current.close()
      }
    }
  }, [programId])

  // ── Derived summary stats ─────────────────────────────────────────────
  const escalationCount = sprints.filter(s =>
    s.health_badge === 'ESCALATE' || s.worst_severity === 'CRITICAL'
  ).length
  const flaggedCount = tickets.filter(t => t.risk_flag).length
  const activeRisks  = tickets.filter(t => t.risk_severity).length
  const critHighCount = tickets.filter(t =>
    t.risk_severity === 'CRITICAL' || t.risk_severity === 'HIGH'
  ).length

  // worst badge across all sprints
  const BADGE_ORDER = ['ESCALATE', 'ALERT', 'WATCH', 'HEALTHY']
  const worstBadge = sprints.reduce((worst, s) => {
    const idx = BADGE_ORDER.indexOf(s.health_badge)
    const widx = BADGE_ORDER.indexOf(worst)
    return idx !== -1 && (widx === -1 || idx < widx) ? s.health_badge : worst
  }, null)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <Topbar
        program={program}
        wsStatus={wsStatus}
        lastCycle={lastCycle}
        escalationCount={escalationCount}
      />
      <SummaryBar
        sprints={sprints}
        worstBadge={worstBadge}
        activeRisks={activeRisks}
        critHighCount={critHighCount}
        flaggedCount={flaggedCount}
        totalTickets={tickets.length}
        program={program}
      />
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <Sidebar
          activePanel={activePanel}
          onSelect={setActivePanel}
          escalationCount={escalationCount}
          flaggedCount={flaggedCount}
          decisionCount={decisions.length}
        />
        <main style={{ flex: 1, overflowY: 'auto', padding: '32px 40px' }}>
          {activePanel === 'health' && (
            <ProgramHealth sprints={sprints} tickets={tickets} />
          )}
          {activePanel === 'backlog' && (
            <BacklogView tickets={tickets} />
          )}
          {activePanel === 'activity' && (
            <AgentActivity decisions={decisions} />
          )}
          {activePanel === 'outputs' && (
            <ExecutiveOutputs outputs={outputs} lastCycle={lastCycle} program={program} />
          )}
        </main>
      </div>
    </div>
  )
}
