import type { ActionFunctionArgs } from "@remix-run/node";
import Stripe from "stripe";

type PaymentJob = {
  eventId: string;
  paymentIntentId: string;
  livemode: boolean;
};

// TODO: In one database transaction, insert the event ID with a
// uniqueness constraint and enqueue the job. Treat an already
// recorded event as success; do not create a second job.
async function saveEventAndQueue(_job: PaymentJob): Promise<void> {
  throw new Error("Durable event queue not implemented");
}

export async function action({ request }: ActionFunctionArgs) {
  const apiKey = process.env.STRIPE_SECRET_KEY;
  const signingSecret = process.env.STRIPE_WEBHOOK_SECRET;
  const signature = request.headers.get("stripe-signature");

  if (!apiKey || !signingSecret) {
    return new Response("Webhook not configured", { status: 500 });
  }
  if (!signature) {
    return new Response("Missing signature", { status: 400 });
  }

  const stripe = new Stripe(apiKey);
  let event: Stripe.Event;

  try {
    // Do not parse or modify the body before verification.
    const rawBody = await request.text();
    event = stripe.webhooks.constructEvent(rawBody, signature, signingSecret);
  } catch {
    return new Response("Invalid webhook", { status: 400 });
  }

  if (event.type === "payment_intent.succeeded") {
    const intent = event.data.object as Stripe.PaymentIntent;

    try {
      await saveEventAndQueue({
        eventId: event.id,
        paymentIntentId: intent.id,
        livemode: event.livemode,
      });
    } catch {
      return new Response("Temporary persistence failure", { status: 500 });
    }
  }

  return new Response("Received", { status: 200 });
}
