import { describe, expect, it } from 'vitest'
import { parseAuthMode } from './authMode'

describe('parseAuthMode', () => {
  it('accepts the two supported modes', () => {
    expect(parseAuthMode('local')).toBe('local')
    expect(parseAuthMode('none')).toBe('none')
  })

  it('has no default: unset or unknown values are null', () => {
    expect(parseAuthMode(undefined)).toBeNull()
    expect(parseAuthMode('')).toBeNull()
    expect(parseAuthMode('proxy')).toBeNull()
  })
})
