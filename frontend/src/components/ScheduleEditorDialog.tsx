import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

export type ScheduleType = 'daily' | 'weekdays' | 'interval' | 'cron'

export interface ScheduleConfig {
  type: ScheduleType
  time?: string
  interval?: number
  cron?: string
}

export interface SchedulePreview {
  schedule: string
  timezone: string
  next_runs: string[]
}

interface ScheduleEditorDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title?: string
  description?: string
  schedule: string
  onSave: (schedule: string) => Promise<void> | void
}

export function parseCronToConfig(cron: string): ScheduleConfig {
  if (!cron) return { type: 'daily', time: '15:30' }

  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return { type: 'cron', cron }

  const [minute, hour, , , dayOfWeek] = parts

  if (minute.startsWith('*/')) {
    const interval = parseInt(minute.slice(2), 10)
    if (!Number.isNaN(interval)) {
      return { type: 'interval', interval }
    }
  }

  const parsedMinute = parseInt(minute, 10)
  const parsedHour = parseInt(hour, 10)
  if (!Number.isNaN(parsedMinute) && !Number.isNaN(parsedHour)) {
    const time = `${parsedHour.toString().padStart(2, '0')}:${parsedMinute.toString().padStart(2, '0')}`
    if (dayOfWeek === '1-5') return { type: 'weekdays', time }
    if (dayOfWeek === '*') return { type: 'daily', time }
  }

  return { type: 'cron', cron }
}

export function configToCron(config: ScheduleConfig): string {
  switch (config.type) {
    case 'daily': {
      const [hour, minute] = (config.time || '15:30').split(':')
      return `${parseInt(minute, 10)} ${parseInt(hour, 10)} * * *`
    }
    case 'weekdays': {
      const [hour, minute] = (config.time || '15:30').split(':')
      return `${parseInt(minute, 10)} ${parseInt(hour, 10)} * * 1-5`
    }
    case 'interval':
      return `*/${config.interval || 30} * * * *`
    case 'cron':
      return config.cron || '0 15 * * *'
    default:
      return '0 15 * * *'
  }
}

export function formatSchedule(cron: string): string {
  const config = parseCronToConfig(cron)
  switch (config.type) {
    case 'daily':
      return `每天 ${config.time}`
    case 'weekdays':
      return `工作日 ${config.time}`
    case 'interval':
      return `每 ${config.interval} 分钟`
    case 'cron':
      return cron || '未设置'
    default:
      return cron || '未设置'
  }
}

function formatPreviewTime(iso: string, timezone?: string): string {
  try {
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return iso
    return date.toLocaleString('zh-CN', {
      timeZone: timezone || undefined,
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return iso
  }
}

export default function ScheduleEditorDialog({
  open,
  onOpenChange,
  title = '设置执行周期',
  description = '',
  schedule,
  onSave,
}: ScheduleEditorDialogProps) {
  const [scheduleConfig, setScheduleConfig] = useState<ScheduleConfig>(() => parseCronToConfig(schedule))
  const [preview, setPreview] = useState<SchedulePreview | { error: string } | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    if (!open) return
    setScheduleConfig(parseCronToConfig(schedule))
  }, [open, schedule])

  useEffect(() => {
    if (!open) {
      setPreview(null)
      return
    }

    const cron = configToCron(scheduleConfig)
    const timer = setTimeout(async () => {
      setPreviewLoading(true)
      try {
        const data = await fetchAPI<SchedulePreview>(`/agents/schedule/preview?schedule=${encodeURIComponent(cron)}&count=5`)
        setPreview(data)
      } catch (error) {
        setPreview({ error: error instanceof Error ? error.message : '预览失败' })
      } finally {
        setPreviewLoading(false)
      }
    }, 300)

    return () => clearTimeout(timer)
  }, [open, scheduleConfig])

  const handleSave = async () => {
    const cron = configToCron(scheduleConfig)
    setSaving(true)
    try {
      await onSave(cron)
      onOpenChange(false)
    } catch (error) {
      toast(error instanceof Error ? error.message : '保存失败', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4 mt-2">
          <div>
            <Label>调度类型</Label>
            <Select value={scheduleConfig.type} onValueChange={value => setScheduleConfig({ ...scheduleConfig, type: value as ScheduleType })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="daily">每天定时</SelectItem>
                <SelectItem value="weekdays">工作日定时</SelectItem>
                <SelectItem value="interval">固定间隔</SelectItem>
                <SelectItem value="cron">自定义 Cron</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {(scheduleConfig.type === 'daily' || scheduleConfig.type === 'weekdays') && (
            <div>
              <Label>执行时间</Label>
              <Input
                type="time"
                value={scheduleConfig.time || '15:30'}
                onChange={event => setScheduleConfig({ ...scheduleConfig, time: event.target.value })}
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                {scheduleConfig.type === 'weekdays' ? '周一到周五' : '每天'}在此时间执行
              </p>
            </div>
          )}

          {scheduleConfig.type === 'interval' && (
            <div>
              <Label>执行间隔（分钟）</Label>
              <Select
                value={String(scheduleConfig.interval || 30)}
                onValueChange={value => setScheduleConfig({ ...scheduleConfig, interval: parseInt(value, 10) })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="5">每 5 分钟</SelectItem>
                  <SelectItem value="10">每 10 分钟</SelectItem>
                  <SelectItem value="15">每 15 分钟</SelectItem>
                  <SelectItem value="30">每 30 分钟</SelectItem>
                  <SelectItem value="60">每小时</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}

          {scheduleConfig.type === 'cron' && (
            <div>
              <Label>Cron 表达式</Label>
              <Input
                value={scheduleConfig.cron || ''}
                onChange={event => setScheduleConfig({ ...scheduleConfig, cron: event.target.value })}
                placeholder="0 15 * * 1-5"
                className="font-mono"
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                格式：分 时 日 月 周，例如 `0 15 * * 1-5`
              </p>
            </div>
          )}

          <div className="rounded-lg border border-border/50 bg-accent/20 p-3">
            <div className="flex items-center justify-between">
              <div className="text-[12px] font-medium text-foreground">未来触发时间预览</div>
              {previewLoading && (
                <span className="w-3.5 h-3.5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
              )}
            </div>

            {'error' in (preview || {}) ? (
              <div className="mt-2 text-[11px] text-muted-foreground">{(preview as { error: string }).error}</div>
            ) : (preview as SchedulePreview | null)?.next_runs?.length ? (
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                {(preview as SchedulePreview).next_runs.map((value, index) => (
                  <span key={index} className="px-1.5 py-0.5 rounded border border-border/60 bg-background/40 font-mono" title={value}>
                    {formatPreviewTime(value, (preview as SchedulePreview).timezone)}
                  </span>
                ))}
                {(preview as SchedulePreview).timezone ? (
                  <span className="opacity-60">({(preview as SchedulePreview).timezone})</span>
                ) : null}
              </div>
            ) : (
              <div className="mt-2 text-[11px] text-muted-foreground">暂无预览</div>
            )}

            <div className="mt-2 text-[11px] text-muted-foreground/70 font-mono">
              schedule: {configToCron(scheduleConfig)}
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
              取消
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? '保存中...' : '保存'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
