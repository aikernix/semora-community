import type { ClarificationQuestion } from './api'

// Machine option values remain in the submission, never in the free-text box.
export function freeAnswerText(question: ClarificationQuestion, answer = '') {
  return question.options.some(option => option.value === answer || option.id === answer) ? '' : answer
}

export function visibleQuestions(questions: ClarificationQuestion[], answers: Record<string, string>) {
  const byId = new Map(questions.map(q => [q.id, q]))
  const canonical = (id: string) => {
    const raw = (answers[id] || '').trim()
    return byId.get(id)?.options.find(o => o.id === raw || o.value === raw)?.value || raw
  }
  const active = (q: ClarificationQuestion, seen: Set<string>): boolean => {
    if (seen.has(q.id)) return false
    const next = new Set([...seen, q.id])
    return (q.depends_on || []).every(c => {
      const parent = byId.get(c.question_id)
      return !!parent && active(parent, next) && c.values.includes(canonical(c.question_id))
    })
  }
  return questions.filter(q => active(q, new Set()))
}
