/**
 * StartOS manifest sketch for bitcoin-tax-tracker (SDK @start9labs/start-sdk, 0.4.x).
 *
 * This is a STARTING POINT — validate against the current hello-world template, since
 * the SDK API evolves. The shape: one Python container, a data volume, a UI interface
 * on :8000, and an optional electrs dependency for xpub sync.
 */
import { setupManifest } from '@start9labs/start-sdk'

export const manifest = setupManifest({
  id: 'bitcoin-tax-tracker',
  title: 'Bitcoin Tax Tracker',
  license: 'MIT',
  wrapperRepo: 'https://github.com/arcasats/bitcoin-tax-tracker',
  upstreamRepo: 'https://github.com/arcasats/bitcoin-tax-tracker',
  supportSite: '',
  marketingSite: '',
  donationUrl: null,
  description: {
    short: 'Local-only Bitcoin tax & accounting',
    long: 'Track Bitcoin transactions by account (xpub + CSV + read-only exchange APIs), '
        + 'compute per-account FIFO cost basis, and generate US Form 8949 / Schedule D — '
        + 'on your own node. No cloud, no accounts; your coin data stays on the box (the only '
        + 'outbound traffic is a public BTC/USD price feed you can disable).',
  },
  volumes: ['data'],
  images: {
    main: { source: { dockerTag: 'bitcoin-tax-tracker:latest' } },
  },
  hardwareRequirements: {},
  alerts: {},
  dependencies: {
    // Optional: electrs/Electrum server for xpub on-chain scanning.
    electrs: {
      description: 'Used to scan xpub addresses for on-chain history.',
      optional: true,
      s9pk: '',
    },
  },
})
