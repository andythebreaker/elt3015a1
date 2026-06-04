module obd_can_node #(
    parameter integer CLK_HZ = 100_000_000,
    parameter integer CAN_BITRATE = 500_000,
    parameter integer UART_BAUD = 115200
) (
    input  wire clk,
    input  wire reset,
    input  wire can_rx,
    output wire can_tx,
    input  wire uart_rx,
    output wire uart_tx
);

// OBD-II CAN IDs
localparam [10:0] OBD_ID_FUNC = 11'h7DF; // Functional broadcast request
localparam [10:0] OBD_ID_PHYS = 11'h7E0; // Physical request to ECU #1
localparam [10:0] OBD_ID_RESP = 11'h7E8; // Response from ECU #1

// Dummy sensor data
localparam [15:0] RPM_RAW   = 16'd12000; // 3000 RPM * 4
localparam [7:0]  SPEED_KPH = 8'd88;     // 88 km/h

// Supported PIDs bitmask for PID 00 response
// Supports PID 0C (RPM) and PID 0D (Speed)
// PID 0C -> bit (32-12)=20, PID 0D -> bit (32-13)=19
// Combined: 0x00180000
localparam [31:0] SUPPORTED_PIDS = 32'h00180000;

// ----- CAN controller interface -----
wire        rx_valid;
wire [10:0] rx_id;
wire [3:0]  rx_dlc;
wire [63:0] rx_data;

reg         tx_start = 1'b0;
reg  [10:0] tx_id    = 11'd0;
reg  [3:0]  tx_dlc   = 4'd0;
reg  [63:0] tx_data  = 64'd0;
wire        tx_busy;
wire        tx_done;

// ----- Response state -----
reg       resp_pending = 1'b0;
reg [7:0] resp_pid     = 8'd0;

// Convenience wires for received data bytes
wire [7:0] rx_b0 = rx_data[63:56]; // PCI byte
wire [7:0] rx_b1 = rx_data[55:48]; // Service ID (0x01 = current data)
wire [7:0] rx_b2 = rx_data[47:40]; // PID

wire [7:0] rpm_a = RPM_RAW[15:8];
wire [7:0] rpm_b = RPM_RAW[7:0];

wire is_obd_id  = (rx_id == OBD_ID_FUNC) || (rx_id == OBD_ID_PHYS);
wire is_obd_req = is_obd_id && (rx_dlc >= 4'd3) && (rx_b0 == 8'h02) && (rx_b1 == 8'h01);
wire is_pid_supported = (rx_b2 == 8'h00) || (rx_b2 == 8'h0C) || (rx_b2 == 8'h0D);

// ----- Debug UART interface -----
reg        dbg_start = 1'b0;
reg  [7:0] dbg_byte  = 8'd0;
wire       dbg_busy;

// ----- Debug output state -----
reg [3:0] out_state   = 4'd0;
reg [1:0] out_kind    = 2'd0;  // 1=R (obd request log), 2=H (heartbeat)
reg       dbg_pending = 1'b0;
reg [7:0] dbg_pid     = 8'd0;

// CAN RX edge detection (runs continuously)
reg can_rx_d1 = 1'b1;
reg can_rx_d2 = 1'b1;
reg rx_edge_seen_latched = 1'b0;

wire core_debug_rx_start;
wire core_debug_rx_ack_slot;
wire core_debug_rx_crc_ok;
wire core_debug_rx_crc_error;
wire core_debug_rx_stuff_error;
wire core_debug_tx_ack_seen;

reg core_rx_start_seen = 1'b0;
reg core_rx_ack_seen = 1'b0;
reg core_rx_crc_ok_seen = 1'b0;
reg core_rx_crc_error_seen = 1'b0;
reg core_rx_stuff_error_seen = 1'b0;

// TX event tracking (cleared each heartbeat)
reg tx_attempted = 1'b0;
reg tx_ack_seen_latched = 1'b0;
reg tx_done_seen = 1'b0;

// Heartbeat timer (1 second)
localparam integer HB_INTERVAL = CLK_HZ;
reg [31:0] hb_cnt     = 32'd0;
reg        hb_pending = 1'b0;

// Periodic test TX timer (2 seconds) - bus ACK diagnostic
localparam TEST_TX_ENABLE = 1'b1;
localparam integer TEST_TX_INTERVAL = CLK_HZ * 2;
reg [31:0] test_tx_cnt = 32'd0;

// =============================================
// Instantiate CAN controller
// =============================================
can_simple_controller #(
    .CLK_HZ(CLK_HZ),
    .CAN_BITRATE(CAN_BITRATE)
) can_core (
    .clk(clk),
    .reset(reset),
    .can_rx(can_rx),
    .can_tx(can_tx),
    .tx_start(tx_start),
    .tx_id(tx_id),
    .tx_dlc(tx_dlc),
    .tx_data(tx_data),
    .tx_busy(tx_busy),
    .tx_done(tx_done),
    .rx_valid(rx_valid),
    .rx_id(rx_id),
    .rx_dlc(rx_dlc),
    .rx_data(rx_data),
    .debug_rx_start(core_debug_rx_start),
    .debug_rx_ack_slot(core_debug_rx_ack_slot),
    .debug_rx_crc_ok(core_debug_rx_crc_ok),
    .debug_rx_crc_error(core_debug_rx_crc_error),
    .debug_rx_stuff_error(core_debug_rx_stuff_error),
    .debug_tx_ack_seen(core_debug_tx_ack_seen)
);

// =============================================
// Instantiate debug UART TX
// =============================================
uart_tx #(
    .CLK_HZ(CLK_HZ),
    .BAUD(UART_BAUD)
) dbg_uart (
    .clk(clk),
    .reset(reset),
    .tx_start(dbg_start),
    .tx_data(dbg_byte),
    .tx_busy(dbg_busy),
    .tx_line(uart_tx)
);

// =============================================
// OBD Response + Periodic Test TX
// =============================================
always @(posedge clk) begin
    if (reset) begin
        resp_pending <= 1'b0;
        resp_pid     <= 8'd0;
        tx_start     <= 1'b0;
        tx_id        <= 11'd0;
        tx_dlc       <= 4'd0;
        tx_data      <= 64'd0;
        test_tx_cnt  <= 32'd0;
    end else begin
        tx_start <= 1'b0;

        if (TEST_TX_ENABLE && test_tx_cnt >= TEST_TX_INTERVAL - 1) begin
            test_tx_cnt <= 32'd0;
            if (!resp_pending && !tx_busy) begin
                resp_pending <= 1'b1;
                resp_pid     <= 8'h0C;
            end
        end else begin
            test_tx_cnt <= test_tx_cnt + 1'b1;
        end

        if (rx_valid && is_obd_req && is_pid_supported) begin
            resp_pending <= 1'b1;
            resp_pid     <= rx_b2;
        end

        if (resp_pending && !tx_busy) begin
            tx_id  <= OBD_ID_RESP;
            tx_dlc <= 4'd8;
            case (resp_pid)
                8'h00: tx_data <= {8'h06, 8'h41, 8'h00,
                                   SUPPORTED_PIDS[31:24], SUPPORTED_PIDS[23:16],
                                   SUPPORTED_PIDS[15:8],  SUPPORTED_PIDS[7:0],
                                   8'h00};
                8'h0C: tx_data <= {8'h04, 8'h41, 8'h0C, rpm_a, rpm_b,
                                   8'h00, 8'h00, 8'h00};
                8'h0D: tx_data <= {8'h03, 8'h41, 8'h0D, SPEED_KPH,
                                   8'h00, 8'h00, 8'h00, 8'h00};
                default: tx_data <= 64'd0;
            endcase
            tx_start     <= 1'b1;
            resp_pending <= 1'b0;
        end
    end
end

// =============================================
// Debug UART Output (heartbeat + OBD debug)
// =============================================
// Heartbeat format: H<rx><edge><sof><ack><ok><crc_err><stuff><att><tx_ack><done><busy>\n
//   rx      = CAN RX pin level ('0'/'1')
//   edge    = any edge detected since last heartbeat ('0'/'1')
//   sof     = receiver saw a start-of-frame edge ('0'/'1')
//   ack     = receiver reached the ACK slot ('0'/'1')
//   ok      = receiver saw a CRC-valid standard data frame ('0'/'1')
//   crc_err = receiver saw a CRC/format-rejected frame ('0'/'1')
//   stuff   = receiver saw a bit-stuffing error ('0'/'1')
//   att     = TX was attempted since last heartbeat ('0'/'1')
//   tx_ack  = another CAN node ACKed our transmitted frame ('0'/'1')
//   done    = TX completed since last heartbeat ('0'/'1')
//   busy    = TX currently busy right now ('0'/'1')
//
// OBD debug format: R<pid_byte>\n  (3 bytes, pid as raw hex)
always @(posedge clk) begin
    if (reset) begin
        dbg_pending          <= 1'b0;
        dbg_pid              <= 8'd0;
        dbg_start            <= 1'b0;
        dbg_byte             <= 8'd0;
        out_state            <= 4'd0;
        out_kind             <= 2'd0;
        hb_cnt               <= 32'd0;
        hb_pending           <= 1'b0;
        can_rx_d1            <= 1'b1;
        can_rx_d2            <= 1'b1;
        rx_edge_seen_latched     <= 1'b0;
        core_rx_start_seen      <= 1'b0;
        core_rx_ack_seen        <= 1'b0;
        core_rx_crc_ok_seen     <= 1'b0;
        core_rx_crc_error_seen  <= 1'b0;
        core_rx_stuff_error_seen <= 1'b0;
        tx_attempted            <= 1'b0;
        tx_ack_seen_latched     <= 1'b0;
        tx_done_seen            <= 1'b0;
    end else begin
        dbg_start <= 1'b0;

        // --- CAN RX edge detection ---
        can_rx_d1 <= can_rx;
        can_rx_d2 <= can_rx_d1;
        if (can_rx_d1 != can_rx_d2) begin
            rx_edge_seen_latched <= 1'b1;
        end

        // --- Track CAN RX parser events ---
        if (core_debug_rx_start) begin
            core_rx_start_seen <= 1'b1;
        end
        if (core_debug_rx_ack_slot) begin
            core_rx_ack_seen <= 1'b1;
        end
        if (core_debug_rx_crc_ok) begin
            core_rx_crc_ok_seen <= 1'b1;
        end
        if (core_debug_rx_crc_error) begin
            core_rx_crc_error_seen <= 1'b1;
        end
        if (core_debug_rx_stuff_error) begin
            core_rx_stuff_error_seen <= 1'b1;
        end

        // --- Track TX events ---
        if (tx_start) begin
            tx_attempted <= 1'b1;
        end
        if (core_debug_tx_ack_seen) begin
            tx_ack_seen_latched <= 1'b1;
        end
        if (tx_done) begin
            tx_done_seen <= 1'b1;
        end

        // --- Heartbeat timer (1 second) ---
        if (hb_cnt >= HB_INTERVAL - 1) begin
            hb_cnt     <= 32'd0;
            hb_pending <= 1'b1;
        end else begin
            hb_cnt <= hb_cnt + 1'b1;
        end

        // --- OBD request debug trigger ---
        if (rx_valid && is_obd_req && is_pid_supported) begin
            dbg_pending <= 1'b1;
            dbg_pid     <= rx_b2;
        end

        // --- UART output state machine ---
        case (out_state)
            4'd0: begin // IDLE - pick message type
                if (!dbg_busy && !dbg_start) begin
                    if (dbg_pending) begin
                        out_kind  <= 2'd1; // R-type
                        dbg_byte  <= 8'h52; // 'R'
                        dbg_start <= 1'b1;
                        out_state <= 4'd1;
                    end else if (hb_pending) begin
                        out_kind  <= 2'd2; // H-type
                        dbg_byte  <= 8'h48; // 'H'
                        dbg_start <= 1'b1;
                        out_state <= 4'd1;
                    end
                end
            end

            4'd1: begin // Byte 2
                if (!dbg_busy && !dbg_start) begin
                    if (out_kind == 2'd1) begin
                        // R-type: send PID byte (raw)
                        dbg_byte  <= dbg_pid;
                        dbg_start <= 1'b1;
                        out_state <= 4'd7; // -> newline
                    end else begin
                        // H-type: send CAN RX level
                        dbg_byte  <= can_rx_d2 ? 8'h31 : 8'h30;
                        dbg_start <= 1'b1;
                        out_state <= 4'd2;
                    end
                end
            end

            4'd2: begin // Byte 3 (H-type: edge_seen)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= rx_edge_seen_latched ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd3;
                end
            end

            4'd3: begin // Byte 4 (H-type: SOF seen)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= core_rx_start_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd4;
                end
            end

            4'd4: begin // Byte 5 (H-type: ACK slot reached)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= core_rx_ack_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd5;
                end
            end

            4'd5: begin // Byte 6 (H-type: CRC OK)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= core_rx_crc_ok_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd6;
                end
            end

            4'd6: begin // Byte 7 (H-type: CRC error)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= core_rx_crc_error_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd9;
                end
            end

            4'd9: begin // Byte 8 (H-type: stuff error)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= core_rx_stuff_error_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd10;
                end
            end

            4'd10: begin // Byte 9 (H-type: tx_attempted)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= tx_attempted ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd11;
                end
            end

            4'd11: begin // Byte 10 (H-type: tx_ack_seen)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= tx_ack_seen_latched ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd12;
                end
            end

            4'd12: begin // Byte 11 (H-type: tx_done_seen)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= tx_done_seen ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd13;
                end
            end

            4'd13: begin // Byte 12 (H-type: tx_busy)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= tx_busy ? 8'h31 : 8'h30;
                    dbg_start <= 1'b1;
                    out_state <= 4'd7; // -> newline
                end
            end

            4'd7: begin // Newline (shared by R and H)
                if (!dbg_busy && !dbg_start) begin
                    dbg_byte  <= 8'h0A; // '\n'
                    dbg_start <= 1'b1;
                    out_state <= 4'd8;
                end
            end

            4'd8: begin // Done - clear flags
                if (!dbg_busy && !dbg_start) begin
                    out_state <= 4'd0;
                    if (out_kind == 2'd1) begin
                        dbg_pending <= 1'b0;
                    end else begin
                        hb_pending              <= 1'b0;
                        rx_edge_seen_latched    <= 1'b0;
                        core_rx_start_seen      <= 1'b0;
                        core_rx_ack_seen        <= 1'b0;
                        core_rx_crc_ok_seen     <= 1'b0;
                        core_rx_crc_error_seen  <= 1'b0;
                        core_rx_stuff_error_seen <= 1'b0;
                        tx_attempted            <= 1'b0;
                        tx_ack_seen_latched     <= 1'b0;
                        tx_done_seen            <= 1'b0;
                    end
                    out_kind <= 2'd0;
                end
            end

            default: begin
                out_state <= 4'd0;
                out_kind  <= 2'd0;
            end
        endcase
    end
end

endmodule
