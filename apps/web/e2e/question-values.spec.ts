import { test, expect } from '@playwright/test'
import { freeAnswerText, visibleQuestions } from '../src/queryQuestions'
import type { ClarificationQuestion } from '../src/api'

// Pure contract tests: no browser fixture or browser process is used.
test('internal selection activates the right branch without entering its code in free text', () => {
  const questions: ClarificationQuestion[] = [
    { id: 'scope', field: 'scope', question: '希望统计哪些对象？', reason: '这会改变统计范围。',
      options: [{ id: 'current', value: 'STATE_01', label: '当前仍有效的对象', description: '已结束的不计入' }],
      allow_free_text: true, blocking: true, depends_on: [] },
    { id: 'time', field: 'time', question: '按什么时间统计？', reason: '这会改变所覆盖的记录。', options: [],
      allow_free_text: true, blocking: true, depends_on: [{ question_id: 'scope', values: ['STATE_01'] }] },
  ]
  const answers = { scope: 'STATE_01' }
  expect(freeAnswerText(questions[0], answers.scope)).toBe('')
  expect(answers.scope).toBe('STATE_01')
  expect(visibleQuestions(questions, answers).map(q => q.id)).toEqual(['scope', 'time'])
  answers.scope = '也包含此前结束的对象'
  expect(freeAnswerText(questions[0], answers.scope)).toBe('也包含此前结束的对象')
  expect(visibleQuestions(questions, answers).map(q => q.id)).toEqual(['scope'])
})
