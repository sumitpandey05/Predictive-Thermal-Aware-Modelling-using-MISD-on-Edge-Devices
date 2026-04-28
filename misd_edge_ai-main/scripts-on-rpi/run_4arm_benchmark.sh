run_test() {
    NAME=$1
    GOV_LOAD=$2
    GOV_UNLOAD=$3

    echo "--- Arm: $NAME ---"
    eval "$GOV_LOAD"
    
    # Start llama-server (background)
    ~/llama.cpp/llama-server -m ~/models/tinyllama.gguf --port 8080 --threads 4 > /dev/null 2>&1 &
    SERVER_PID=$!
    sleep 10 # Let model load
    
    # Trigger a 10-minute infinite generation
    curl -X POST http://localhost:8080/completion \
         -H "Content-Type: application/json" \
         -d '{"prompt": "Repeat the following word: AI ", "n_predict": 5000}' > /dev/null &
    
    # Start our new Python Telemetry
    python3 tps_telemetry.py "./bench_results/${NAME}_telemetry.csv" &
    TELEMETRY_PID=$!

    sleep 600
    
    # Cleanup
    kill $SERVER_PID $TELEMETRY_PID
    eval "$GOV_UNLOAD"
    sleep 120 # Cooldown
}