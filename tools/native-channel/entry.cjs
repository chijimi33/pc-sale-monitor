(async () => {
  try {
    const {controller} = await import('./artifact.mjs');
    await controller();
  } catch (error) {
    // Only our fixed error codes, never SDK URLs or response bodies.
    const message = /^[a-z_]+$/.test(error.message) ? error.message : 'native_channel_failed';
    console.error(message);
    process.exitCode = 1;
  }
})();
