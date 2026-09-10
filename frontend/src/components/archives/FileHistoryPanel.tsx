import { useMemo } from 'react'
import { Box, Button, Typography, useTheme } from '@mui/material'
import ChangeBadge from './ChangeBadge'
import { changeColor } from './changeStyle'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import PlanGate from '../shared/PlanGate'
import { usePlan } from '../../hooks/usePlan'
import { archivesAPI } from '../../services/api'
import { formatBytes, parseBackendDate } from '../../utils/dateUtils'
import type { HistoryEntry } from '../../types/archives'

interface FileHistoryPanelProps {
  repositoryId: number
  path: string | null
  onRestoreEntry: (entry: HistoryEntry) => void
}

function FileHistoryPanelContent({ repositoryId, path, onRestoreEntry }: FileHistoryPanelProps) {
  const { t } = useTranslation()

  const { data } = useQuery({
    queryKey: ['path-history', repositoryId, path],
    queryFn: () => archivesAPI.getPathHistory(repositoryId, path as string).then((res) => res.data),
    enabled: !!path,
  })

  const series = data?.entries[0]?.series ?? null

  const { data: seriesArchives } = useQuery({
    queryKey: ['archive-series-for-history', repositoryId, series],
    queryFn: () =>
      archivesAPI.listStored(repositoryId, { series: series as string }).then((res) => res.data),
    enabled: !!series,
  })

  // "Not present in N older archives" is a statement about the archives of
  // the path's series older than its first sighting, so it needs exactly
  // those indexed, not the whole repository (a backup that just landed is
  // still pending and says nothing about the past).
  const { notPresentOlderCount, olderArchivesIndexed } = useMemo(() => {
    if (!data || !seriesArchives) return { notPresentOlderCount: 0, olderArchivesIndexed: true }
    const earliestPresentId = data.present.reduce<number | null>(
      (min, range) => (min === null || range.from_archive_id < min ? range.from_archive_id : min),
      null
    )
    if (earliestPresentId === null) return { notPresentOlderCount: 0, olderArchivesIndexed: true }
    const older = seriesArchives.archives.filter((row) => row.id < earliestPresentId)
    return {
      notPresentOlderCount: older.length,
      olderArchivesIndexed: older.every((row) => row.history_state === 'indexed'),
    }
  }, [data, seriesArchives])

  const entries = data?.entries ?? []
  // What the entries are based on. With nothing indexed they say nothing
  // about the path (the repository is not indexed yet, or cannot be: an
  // agent executes it); with a partial index they cover the indexed
  // archives only, so "no earlier archive contains this path" is only true
  // once every archive is indexed.
  // What the answer is based on. The route's coverage counts the whole
  // repository; once the path's series is known, its own archives decide
  // (the listing carries their states), since every statement below is
  // about that series. A `skipped` archive (an agent's repository) never
  // gets indexed and is no missing progress either way.
  const coverage = useMemo(() => {
    const repositoryWide = data?.coverage
    if (!seriesArchives) return repositoryWide
    const reachable = seriesArchives.archives.filter((row) => row.history_state !== 'skipped')
    return {
      indexed: reachable.filter((row) => row.history_state === 'indexed').length,
      exhausted: repositoryWide?.exhausted ?? 0,
      total: reachable.length,
      capability: repositoryWide?.capability ?? 'available',
    }
  }, [data, seriesArchives])
  // Entries can outlive a reset of the archive states (a series rename puts
  // every archive back to pending without deleting its rows), so the
  // "nothing indexed" answer is given only when there is nothing to show.
  const nothingIndexed =
    coverage != null && coverage.total > 0 && coverage.indexed === 0 && entries.length === 0
  const partiallyIndexed =
    coverage != null && coverage.indexed > 0 && coverage.indexed < coverage.total
  const fullyIndexed = coverage == null || coverage.indexed >= coverage.total
  const sortedEntries = [...entries].sort((a, b) => (a.start < b.start ? 1 : -1))
  const firstAddedId = [...entries]
    .filter((e) => e.change === 'added')
    .sort((a, b) => (a.start < b.start ? -1 : 1))[0]?.archive_id

  const theme = useTheme()

  // With nothing to show for the path, the reason comes first: no index at
  // all, or none that will ever be completed (an agent executes the
  // repository; whatever was indexed on the server before stays partial).
  const unavailableForAgent = coverage?.capability === 'agent_unsupported' && entries.length === 0
  if (data && (nothingIndexed || unavailableForAgent)) {
    return (
      <Box>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {unavailableForAgent
            ? t('archives.files.historyUnavailableAgent')
            : coverage != null && coverage.exhausted >= coverage.total
              ? // the executor has given up on every archive: not "yet"
                t('archives.files.historyFailed')
              : t('archives.files.historyNotIndexed')}
        </Typography>
      </Box>
    )
  }

  return (
    <Box>
      {data && sortedEntries.length === 0 && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {fullyIndexed
            ? t('archives.files.historyEmpty')
            : t('archives.files.historyEmptyIndexed')}
        </Typography>
      )}
      {partiallyIndexed && coverage && (
        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 1 }}>
          {t('archives.files.historyCoverage', {
            indexed: coverage.indexed,
            total: coverage.total,
          })}
        </Typography>
      )}
      <Box>
        {sortedEntries.map((entry) => {
          const isFirst = entry.archive_id === firstAddedId
          const change = isFirst ? 'added' : entry.change === 'summary' ? 'modified' : entry.change
          const detail = isFirst
            ? t('archives.files.firstSeen')
            : entry.change === 'modified'
              ? `${formatBytes(entry.size_before)} → ${formatBytes(entry.size_after)}`
              : t(`archives.changes.${entry.change === 'summary' ? 'modified' : entry.change}`)
          return (
            <Box
              key={entry.archive_id}
              sx={{
                display: 'grid',
                gridTemplateColumns: '20px minmax(0, 1fr) auto',
                columnGap: 1.5,
                alignItems: 'start',
                py: 1.25,
                borderTop: 1,
                borderColor: 'divider',
                '&:first-of-type': { borderTop: 0 },
              }}
            >
              <Box sx={{ pt: 0.25 }}>
                <ChangeBadge change={change} size={18} />
              </Box>
              <Box sx={{ minWidth: 0 }}>
                <Typography
                  variant="body2"
                  noWrap
                  title={entry.archive_name}
                  sx={{ fontWeight: 600 }}
                >
                  {entry.archive_name}
                </Typography>
                <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                  {parseBackendDate(entry.start).toLocaleString()}
                  <Box
                    component="span"
                    sx={{ color: changeColor(theme, change), ml: 1, fontWeight: 600 }}
                  >
                    {detail}
                  </Box>
                </Typography>
              </Box>
              <Button size="small" onClick={() => onRestoreEntry(entry)} sx={{ mt: -0.5 }}>
                {t('archives.files.restoreThis')}
              </Button>
            </Box>
          )
        })}
      </Box>
      {olderArchivesIndexed && notPresentOlderCount > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mt: 1 }}>
          {t('archives.files.notPresent', { count: notPresentOlderCount })}
        </Typography>
      )}
    </Box>
  )
}

export default function FileHistoryPanel(props: FileHistoryPanelProps) {
  const { can } = usePlan()
  return (
    <PlanGate feature="archive_history" disabled surface="archive_files" operation="view_history">
      {can('archive_history') ? (
        <FileHistoryPanelContent {...props} />
      ) : (
        <Box sx={{ minHeight: 60 }} />
      )}
    </PlanGate>
  )
}
