require('dotenv').config();
const express = require('express');
const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use('/api', require('./src/routes'));

import('./src/sellvia-keys.js').then(({ rotateKeyHandler }) => {
  app.post('/admin/sellvia/rotate-key', rotateKeyHandler());
}).catch((error) => console.error('Failed to load Sellvia key manager:', error.message));

import('./src/workers/index.js').then(({ startWorkers, handleStatusRequest }) => {
  app.use((req, res, next) => (handleStatusRequest(req, res) ? undefined : next()));
  startWorkers();
}).catch((error) => console.error('Failed to start background workers:', error.message));

const POLL_INTERVAL_MS = 10 * 60 * 1000;
let isPolling = false;

async function runPollCycle() {
  if (isPolling) {
    console.log('[poll] Previous check still running, skipping overlapping cycle.');
    return;
  }

  isPolling = true;
  try {
    const now = new Date().toISOString();
    const hasStripe = Boolean(process.env.STRIPE_SECRET_KEY);
    const hasShopify = Boolean(process.env.SHOPIFY_ACCESS_TOKEN);
    console.log(`[poll] ${now} — Bridge active (Stripe key: ${hasStripe ? 'ok' : 'missing'}, Shopify token: ${hasShopify ? 'ok' : 'missing'})`);
  } catch (error) {
    console.error('[poll] Error during check cycle:', error.message);
  } finally {
    isPolling = false;
  }
}

setInterval(runPollCycle, POLL_INTERVAL_MS);

app.listen(PORT, () => {
  console.log(`Fabulous Gems server running on port ${PORT}`);
  runPollCycle();
});
