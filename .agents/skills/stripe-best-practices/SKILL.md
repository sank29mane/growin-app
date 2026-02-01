---
name: stripe-best-practices
description: Official Stripe guidelines for designing payment and subscription integrations.
---

# Stripe Best Practices

This skill provides instructions for building secure and reliable financial integrations using Stripe.

## Core Instructions

1. **Checkout UI**: Favor Stripe Checkout for hosted payment pages to minimize security scope.
2. **Webhooks**: Always implement IDempotency and verify webhook signatures.
3. **Subscription Logic**: Use `PaymentIntents` and `SetupIntents` for complex subscription flows.
4. **Error Handling**: Implement graceful retry logic and handle declined payments with clear user feedback.
5. **Testing**: Use the Stripe CLI for local webhook testing and always test with specific card numbers for different failure scenarios.

## Important Links
- [Official Documentation](https://docs.stripe.com)
- [Stripe CLI Guide](https://docs.stripe.com/stripe-cli)
