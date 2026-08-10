// A model badge: "provider/model-id" as a provider mark + the model name.
//
// Provider LOGOS are trademarks, so none are bundled and none are imitated. Drop an
// official SVG at web/public/logos/<provider>.svg and it is used automatically;
// otherwise the badge shows a monogram tinted per provider, which reads fine either
// way. A missing logo is probed once per provider per page, then remembered.

import { useState } from 'react'

const TINTS: Record<string, string> = {
  claude: '#c96442',
  anthropic: '#c96442',
  openai: '#0f9d76',
  kimi: '#6f5bd4',
  moonshot: '#6f5bd4',
  google: '#3b7ddd',
  mistral: '#d97a1a',
  ollama: '#767676',
}

/** Providers whose logo file we already know is absent. */
const missingLogos = new Set<string>()

function tint(provider: string): string {
  if (TINTS[provider]) return TINTS[provider]
  let hash = 0
  for (const ch of provider) hash = (hash * 31 + ch.charCodeAt(0)) % 360
  return `hsl(${hash} 45% 45%)`
}

export function ModelBadge({ model }: { model: string }) {
  const [logoMissing, setLogoMissing] = useState(() =>
    missingLogos.has(model.split('/')[0]),
  )

  // `model: "@<select>.winner_model"` is a binding, not a model (SPEC §8.1): the
  // engine fills it in from the select's winner, so there is no provider to badge.
  if (model.startsWith('@')) {
    const source = model.slice(1).split('.')[0]
    return (
      <span className="chip chip-bind" title={model}>
        winner of {source}
      </span>
    )
  }

  const [provider, ...rest] = model.split('/')
  const name = rest.join('/') || model

  return (
    <span className="model-badge" title={model}>
      {logoMissing ? (
        <span className="model-mark" style={{ background: tint(provider) }}>
          {provider.slice(0, 1).toUpperCase()}
        </span>
      ) : (
        <img
          className="model-logo"
          src={`/logos/${provider}.svg`}
          alt=""
          onError={() => {
            missingLogos.add(provider)
            setLogoMissing(true)
          }}
        />
      )}
      <span className="model-name">{name}</span>
    </span>
  )
}
