import { expect, it } from 'vitest'
import { taskMessage } from './display'

it('explains Aliyun drive quota failure while keeping the diagnostic code', () => {
  const message = taskMessage('Transfer step: copy shared file; Aliyun Drive: copy shared file: request rejected (HTTP 400, Aliyun code=QuotaExhausted.Drive); automatic retries exhausted')
  expect(message).toContain('目标网盘空间不足，请释放空间或扩容后重试')
  expect(message).toContain('QuotaExhausted.Drive')
  expect(message).toContain('HTTP 400')
  expect(message).toContain('自动重试次数已耗尽')
})
