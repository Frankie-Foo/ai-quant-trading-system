function createIbkrPaperAdapter() {
  return {
    async status() {
      return {
        adapter: 'ibkr-paper-reserved.v1',
        configured: false,
        connected: false,
        orderSubmissionEnabled: false,
      }
    },

    async connect() {
      throw new Error('IBKR Paper interface is reserved but not configured')
    },

    async placeOrder() {
      throw new Error('IBKR Paper order submission is disabled in the research edition')
    },
  }
}

module.exports = {
  createIbkrPaperAdapter,
}
