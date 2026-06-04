module can_simple_controller #(
    parameter integer CLK_HZ = 100_000_000,
    parameter integer CAN_BITRATE = 500_000,
    parameter integer TQ_PER_BIT = 8,
    parameter integer SAMPLE_TQ = 6
) (
    input  wire clk,
    input  wire reset,
    input  wire can_rx,
    output wire can_tx,
    input  wire tx_start,
    input  wire [10:0] tx_id,
    input  wire [3:0] tx_dlc,
    input  wire [63:0] tx_data,
    output wire tx_busy,
    output reg  tx_done,
    output reg  rx_valid,
    output reg  [10:0] rx_id,
    output reg  [3:0] rx_dlc,
    output reg  [63:0] rx_data,
    output wire debug_rx_start,
    output wire debug_rx_ack_slot,
    output wire debug_rx_crc_ok,
    output wire debug_rx_crc_error,
    output wire debug_rx_stuff_error,
    output wire debug_tx_ack_seen
);

function integer clog2;
    input integer value;
    integer i;
    begin
        value = value - 1;
        for (i = 0; value > 0; i = i + 1) begin
            value = value >> 1;
        end
        clog2 = i;
    end
endfunction

function [14:0] crc15_next;
    input [14:0] crc_in;
    input        bit_in;
    reg feedback;
    reg [14:0] next_crc;
    begin
        feedback = bit_in ^ crc_in[14];
        next_crc = {crc_in[13:0], 1'b0};
        if (feedback) begin
            next_crc = next_crc ^ 15'h4599;
        end
        crc15_next = next_crc;
    end
endfunction

localparam integer TQ_DIV = (CLK_HZ / (CAN_BITRATE * TQ_PER_BIT));
localparam integer TQ_DIV_W = (TQ_DIV <= 1) ? 1 : clog2(TQ_DIV);
localparam integer TQ_CNT_W = (TQ_PER_BIT <= 1) ? 1 : clog2(TQ_PER_BIT);

localparam [3:0] RX_IDLE      = 4'd0;
localparam [3:0] RX_SOF       = 4'd1;
localparam [3:0] RX_ID        = 4'd2;
localparam [3:0] RX_RTR       = 4'd3;
localparam [3:0] RX_IDE       = 4'd4;
localparam [3:0] RX_R0        = 4'd5;
localparam [3:0] RX_DLC       = 4'd6;
localparam [3:0] RX_DATA      = 4'd7;
localparam [3:0] RX_CRC       = 4'd8;
localparam [3:0] RX_CRC_DELIM = 4'd9;
localparam [3:0] RX_ACK       = 4'd10;
localparam [3:0] RX_ACK_DELIM = 4'd11;
localparam [3:0] RX_EOF       = 4'd12;

localparam [3:0] PREP_SOF       = 4'd0;
localparam [3:0] PREP_ID        = 4'd1;
localparam [3:0] PREP_RTR       = 4'd2;
localparam [3:0] PREP_IDE       = 4'd3;
localparam [3:0] PREP_R0        = 4'd4;
localparam [3:0] PREP_DLC       = 4'd5;
localparam [3:0] PREP_DATA      = 4'd6;
localparam [3:0] PREP_CRC       = 4'd7;
localparam [3:0] PREP_CRC_DELIM = 4'd8;
localparam [3:0] PREP_ACK       = 4'd9;
localparam [3:0] PREP_ACK_DELIM = 4'd10;
localparam [3:0] PREP_EOF       = 4'd11;
localparam [3:0] PREP_DONE      = 4'd12;

reg can_rx_ff1 = 1'b1;
reg can_rx_ff2 = 1'b1;
reg can_rx_prev = 1'b1;
wire can_rx_s = can_rx_ff2;
wire can_rx_fall = (can_rx_prev == 1'b1) && (can_rx_s == 1'b0);

reg [TQ_DIV_W-1:0] tq_div_cnt = {TQ_DIV_W{1'b0}};
reg [TQ_CNT_W-1:0] tq_cnt = {TQ_CNT_W{1'b0}};
wire tq_tick = (TQ_DIV <= 1) ? 1'b1 : (tq_div_cnt == TQ_DIV - 1);
wire sample_tick = tq_tick && (tq_cnt == SAMPLE_TQ - 1);
wire bit_end_tick = tq_tick && (tq_cnt == TQ_PER_BIT - 1);

wire rx_start_pulse;
wire tx_start_pulse;
wire hard_sync_pulse;
wire rx_crc_last_sample;
wire rx_crc_match_now;

reg [1:0] idle_cnt = 2'd0;
wire bus_idle = (idle_cnt == 2'd3);

reg [3:0] rx_state = RX_IDLE;
reg [4:0] rx_bit_cnt = 5'd0;
reg [2:0] rx_byte_cnt = 3'd0;
reg [7:0] rx_byte_shift = 8'd0;
reg [14:0] rx_crc = 15'd0;
reg [14:0] rx_crc_recv = 15'd0;
reg [3:0] rx_dlc_shift = 4'd0;
wire [3:0] rx_dlc_shift_next = {rx_dlc_shift[2:0], can_rx_s};
reg rx_rtr = 1'b0;
reg rx_ide = 1'b0;
reg rx_frame_ok = 1'b0;
reg rx_stuff_error = 1'b0;
reg rx_destuff_en = 1'b0;
reg rx_last_bit = 1'b1;
reg [2:0] rx_same_cnt = 3'd0;
reg rx_skip_bit = 1'b0;

reg prep_active = 1'b0;
reg tx_pending = 1'b0;
reg tx_active = 1'b0;
reg [7:0] tx_buf_len = 8'd0;
reg [7:0] tx_buf_pos = 8'd0;
reg [255:0] tx_buf = 256'd0;
reg tx_bit = 1'b1;
reg [7:0] tx_ack_index = 8'd0;
reg tx_ack_seen = 1'b0;

reg [10:0] tx_msg_id = 11'd0;
reg [3:0] tx_msg_dlc = 4'd0;
reg [63:0] tx_msg_data = 64'd0;

reg [3:0] prep_state = PREP_SOF;
reg [4:0] prep_bit_cnt = 5'd0;
reg [2:0] prep_byte_cnt = 3'd0;
reg [14:0] prep_crc = 15'd0;
reg prep_need_stuff = 1'b0;
reg prep_last_bit = 1'b1;
reg [2:0] prep_same_cnt = 3'd0;
reg prep_bit = 1'b1;
reg prep_stuff_enable = 1'b0;
reg prep_crc_enable = 1'b0;
reg [2:0] prep_same_cnt_next = 3'd0;
reg prep_done_pulse = 1'b0;

assign tx_busy = tx_pending || tx_active || prep_active;

assign rx_start_pulse = (rx_state == RX_IDLE) && !tx_active && can_rx_fall;
assign tx_start_pulse = (!tx_active && tx_pending && bus_idle && rx_state == RX_IDLE && bit_end_tick);
assign hard_sync_pulse = rx_start_pulse || tx_start_pulse;
assign rx_crc_last_sample = sample_tick && (rx_state == RX_CRC) && (rx_bit_cnt == 5'd14);
assign rx_crc_match_now = ({rx_crc_recv[13:0], can_rx_s} == rx_crc) && (rx_ide == 1'b0) && (rx_rtr == 1'b0) && !rx_stuff_error;
assign debug_rx_start = rx_start_pulse;
assign debug_rx_ack_slot = sample_tick && (rx_state == RX_ACK);
assign debug_rx_crc_ok = rx_crc_last_sample && rx_crc_match_now;
assign debug_rx_crc_error = rx_crc_last_sample && !rx_crc_match_now;
assign debug_rx_stuff_error = rx_stuff_error;
assign debug_tx_ack_seen = tx_ack_seen;

reg drive_ack = 1'b0;
always @(posedge clk) begin
    if (reset) begin
        drive_ack <= 1'b0;
    end else if (bit_end_tick) begin
        if (rx_state == RX_ACK && rx_frame_ok) begin
            drive_ack <= 1'b1;
        end else begin
            drive_ack <= 1'b0;
        end
    end
end

assign can_tx = tx_active ? tx_bit : (drive_ack ? 1'b0 : 1'b1);

always @(posedge clk) begin
    if (reset) begin
        can_rx_ff1 <= 1'b1;
        can_rx_ff2 <= 1'b1;
        can_rx_prev <= 1'b1;
    end else begin
        can_rx_ff1 <= can_rx;
        can_rx_ff2 <= can_rx_ff1;
        can_rx_prev <= can_rx_s;
    end
end

always @(posedge clk) begin
    if (reset) begin
        tq_div_cnt <= {TQ_DIV_W{1'b0}};
        tq_cnt <= {TQ_CNT_W{1'b0}};
        
    end else begin
        if (hard_sync_pulse) begin
            tq_div_cnt <= {TQ_DIV_W{1'b0}};
            tq_cnt <= {TQ_CNT_W{1'b0}};
        end else begin
            if (tq_tick) begin
                tq_div_cnt <= {TQ_DIV_W{1'b0}};
                if (bit_end_tick) begin
                    tq_cnt <= {TQ_CNT_W{1'b0}};
                end else begin
                    tq_cnt <= tq_cnt + 1'b1;
                end
            end else begin
                tq_div_cnt <= tq_div_cnt + 1'b1;
            end
        end
    end
end

always @(posedge clk) begin
    if (reset) begin
        idle_cnt <= 2'd0;
    end else if (bit_end_tick) begin
        if (!tx_active && rx_state == RX_IDLE && can_rx_s == 1'b1) begin
            if (idle_cnt != 2'd3) begin
                idle_cnt <= idle_cnt + 1'b1;
            end
        end else begin
            idle_cnt <= 2'd0;
        end
    end
end

always @(posedge clk) begin
    if (reset) begin
        rx_state <= RX_IDLE;
        rx_bit_cnt <= 5'd0;
        rx_byte_cnt <= 3'd0;
        rx_byte_shift <= 8'd0;
        rx_crc <= 15'd0;
        rx_crc_recv <= 15'd0;
        rx_dlc_shift <= 4'd0;
        rx_rtr <= 1'b0;
        rx_ide <= 1'b0;
        rx_frame_ok <= 1'b0;
        rx_stuff_error <= 1'b0;
        rx_destuff_en <= 1'b0;
        rx_last_bit <= 1'b1;
        rx_same_cnt <= 3'd0;
        rx_id <= 11'd0;
        rx_dlc <= 4'd0;
        rx_data <= 64'd0;
        rx_valid <= 1'b0;
    end else begin
        rx_valid <= 1'b0;

        if (rx_start_pulse) begin
            rx_state <= RX_SOF;
            rx_bit_cnt <= 5'd0;
            rx_byte_cnt <= 3'd0;
            rx_byte_shift <= 8'd0;
            rx_crc <= 15'd0;
            rx_crc_recv <= 15'd0;
            rx_dlc_shift <= 4'd0;
            rx_rtr <= 1'b0;
            rx_ide <= 1'b0;
            rx_frame_ok <= 1'b0;
            rx_stuff_error <= 1'b0;
            rx_destuff_en <= 1'b1;
            rx_last_bit <= 1'b1;
            rx_same_cnt <= 3'd0;
            rx_id <= 11'd0;
            rx_dlc <= 4'd0;
            rx_data <= 64'd0;
        end

        if (sample_tick && rx_state != RX_IDLE && !tx_active) begin
            rx_skip_bit = 1'b0;

            if (rx_destuff_en) begin
                if (rx_same_cnt == 3'd5) begin
                    if (can_rx_s == rx_last_bit) begin
                        rx_stuff_error <= 1'b1;
                    end
                    rx_last_bit <= can_rx_s;
                    rx_same_cnt <= 3'd1;
                    rx_skip_bit = 1'b1;
                end else begin
                    if (can_rx_s == rx_last_bit) begin
                        rx_same_cnt <= rx_same_cnt + 1'b1;
                    end else begin
                        rx_same_cnt <= 3'd1;
                        rx_last_bit <= can_rx_s;
                    end
                end
            end else begin
                rx_last_bit <= can_rx_s;
                rx_same_cnt <= 3'd1;
            end

            if (!rx_skip_bit) begin
                case (rx_state)
                    RX_SOF: begin
                        if (can_rx_s == 1'b0) begin
                            rx_crc <= crc15_next(rx_crc, can_rx_s);
                            rx_state <= RX_ID;
                            rx_bit_cnt <= 5'd0;
                        end else begin
                            rx_state <= RX_IDLE;
                        end
                    end
                    RX_ID: begin
                        rx_id <= {rx_id[9:0], can_rx_s};
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        if (rx_bit_cnt == 5'd10) begin
                            rx_state <= RX_RTR;
                            rx_bit_cnt <= 5'd0;
                        end else begin
                            rx_bit_cnt <= rx_bit_cnt + 1'b1;
                        end
                    end
                    RX_RTR: begin
                        rx_rtr <= can_rx_s;
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        rx_state <= RX_IDE;
                    end
                    RX_IDE: begin
                        rx_ide <= can_rx_s;
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        rx_state <= RX_R0;
                    end
                    RX_R0: begin
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        rx_state <= RX_DLC;
                        rx_bit_cnt <= 5'd0;
                        rx_dlc_shift <= 4'd0;
                    end
                    RX_DLC: begin
                        rx_dlc_shift <= rx_dlc_shift_next;
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        if (rx_bit_cnt == 5'd3) begin
                            rx_bit_cnt <= 5'd0;
                            rx_dlc <= (rx_dlc_shift_next > 4'd8) ? 4'd8 : rx_dlc_shift_next;
                            if (rx_dlc_shift_next == 4'd0) begin
                                rx_state <= RX_CRC;
                            end else begin
                                rx_state <= RX_DATA;
                                rx_byte_cnt <= 3'd0;
                                rx_byte_shift <= 8'd0;
                            end
                        end else begin
                            rx_bit_cnt <= rx_bit_cnt + 1'b1;
                        end
                    end
                    RX_DATA: begin
                        rx_byte_shift <= {rx_byte_shift[6:0], can_rx_s};
                        rx_crc <= crc15_next(rx_crc, can_rx_s);
                        if (rx_bit_cnt == 5'd7) begin
                            case (rx_byte_cnt)
                                3'd0: rx_data[63:56] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd1: rx_data[55:48] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd2: rx_data[47:40] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd3: rx_data[39:32] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd4: rx_data[31:24] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd5: rx_data[23:16] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd6: rx_data[15:8] <= {rx_byte_shift[6:0], can_rx_s};
                                3'd7: rx_data[7:0] <= {rx_byte_shift[6:0], can_rx_s};
                                default: rx_data <= rx_data;
                            endcase
                            rx_bit_cnt <= 5'd0;
                            if (rx_byte_cnt == (rx_dlc - 1'b1)) begin
                                rx_state <= RX_CRC;
                            end else begin
                                rx_byte_cnt <= rx_byte_cnt + 1'b1;
                                rx_byte_shift <= 8'd0;
                            end
                        end else begin
                            rx_bit_cnt <= rx_bit_cnt + 1'b1;
                        end
                    end
                    RX_CRC: begin
                        rx_crc_recv <= {rx_crc_recv[13:0], can_rx_s};
                        if (rx_bit_cnt == 5'd14) begin
                            rx_state <= RX_CRC_DELIM;
                            rx_bit_cnt <= 5'd0;
                            rx_destuff_en <= 1'b0;
                            rx_frame_ok <= ({rx_crc_recv[13:0], can_rx_s} == rx_crc) && (rx_ide == 1'b0) && (rx_rtr == 1'b0) && !rx_stuff_error;
                        end else begin
                            rx_bit_cnt <= rx_bit_cnt + 1'b1;
                        end
                    end
                    RX_CRC_DELIM: begin
                        rx_state <= RX_ACK;
                    end
                    RX_ACK: begin
                        rx_state <= RX_ACK_DELIM;
                    end
                    RX_ACK_DELIM: begin
                        rx_state <= RX_EOF;
                        rx_bit_cnt <= 5'd0;
                    end
                    RX_EOF: begin
                        if (rx_bit_cnt == 5'd6) begin
                            if (rx_frame_ok) begin
                                rx_valid <= 1'b1;
                            end
                            rx_state <= RX_IDLE;
                        end else begin
                            rx_bit_cnt <= rx_bit_cnt + 1'b1;
                        end
                    end
                    default: begin
                        rx_state <= RX_IDLE;
                    end
                endcase
            end
        end
    end
end

always @(posedge clk) begin
    if (reset) begin
        prep_active <= 1'b0;
        prep_state <= PREP_SOF;
        prep_bit_cnt <= 5'd0;
        prep_byte_cnt <= 3'd0;
        prep_crc <= 15'd0;
        prep_need_stuff <= 1'b0;
        prep_last_bit <= 1'b1;
        prep_same_cnt <= 3'd0;
        prep_done_pulse <= 1'b0;
        tx_buf_len <= 8'd0;
        tx_buf <= 256'd0;
        tx_msg_id <= 11'd0;
        tx_msg_dlc <= 4'd0;
        tx_msg_data <= 64'd0;
        tx_ack_index <= 8'd0;
    end else begin
        prep_done_pulse <= 1'b0;
        if (!prep_active && tx_start && !tx_busy) begin
            tx_msg_id <= tx_id;
            tx_msg_dlc <= (tx_dlc > 4'd8) ? 4'd8 : tx_dlc;
            tx_msg_data <= tx_data;
            prep_active <= 1'b1;
            prep_state <= PREP_SOF;
            prep_bit_cnt <= 5'd0;
            prep_byte_cnt <= 3'd0;
            prep_crc <= 15'd0;
            prep_need_stuff <= 1'b0;
            prep_last_bit <= 1'b1;
            prep_same_cnt <= 3'd0;
            tx_buf_len <= 8'd0;
            tx_buf <= 256'd0;
            tx_ack_index <= 8'd0;
        end else if (prep_active) begin
            if (prep_state == PREP_DONE && !prep_need_stuff) begin
                prep_active <= 1'b0;
                prep_done_pulse <= 1'b1;
            end else if (prep_need_stuff) begin
                prep_bit = ~prep_last_bit;
                tx_buf[tx_buf_len] <= prep_bit;
                tx_buf_len <= tx_buf_len + 1'b1;
                prep_last_bit <= prep_bit;
                prep_same_cnt <= 3'd1;
                prep_need_stuff <= 1'b0;
            end else begin
                prep_bit = 1'b1;
                prep_stuff_enable = (prep_state <= PREP_CRC);
                prep_crc_enable = (prep_state <= PREP_DATA);
                prep_same_cnt_next = prep_same_cnt;

                case (prep_state)
                    PREP_SOF: begin
                        prep_bit = 1'b0;
                    end
                    PREP_ID: begin
                        prep_bit = tx_msg_id[10 - prep_bit_cnt];
                    end
                    PREP_RTR: begin
                        prep_bit = 1'b0;
                    end
                    PREP_IDE: begin
                        prep_bit = 1'b0;
                    end
                    PREP_R0: begin
                        prep_bit = 1'b0;
                    end
                    PREP_DLC: begin
                        prep_bit = tx_msg_dlc[3 - prep_bit_cnt];
                    end
                    PREP_DATA: begin
                        prep_bit = tx_msg_data[63 - (prep_byte_cnt * 8) - (prep_bit_cnt)];
                    end
                    PREP_CRC: begin
                        prep_bit = prep_crc[14 - prep_bit_cnt];
                    end
                    PREP_CRC_DELIM: begin
                        prep_bit = 1'b1;
                    end
                    PREP_ACK: begin
                        prep_bit = 1'b1;
                        tx_ack_index <= tx_buf_len;
                    end
                    PREP_ACK_DELIM: begin
                        prep_bit = 1'b1;
                    end
                    PREP_EOF: begin
                        prep_bit = 1'b1;
                    end
                    default: begin
                        prep_bit = 1'b1;
                    end
                endcase

                tx_buf[tx_buf_len] <= prep_bit;
                tx_buf_len <= tx_buf_len + 1'b1;

                if (prep_crc_enable) begin
                    prep_crc <= crc15_next(prep_crc, prep_bit);
                end

                if (prep_stuff_enable) begin
                    if (prep_bit == prep_last_bit) begin
                        prep_same_cnt_next = prep_same_cnt + 1'b1;
                    end else begin
                        prep_same_cnt_next = 3'd1;
                    end
                    prep_last_bit <= prep_bit;
                    prep_same_cnt <= prep_same_cnt_next;
                    if (prep_same_cnt_next == 3'd5) begin
                        prep_need_stuff <= 1'b1;
                    end
                end else begin
                    prep_last_bit <= prep_bit;
                    prep_same_cnt <= 3'd1;
                    prep_need_stuff <= 1'b0;
                end

                case (prep_state)
                    PREP_SOF: begin
                        prep_state <= PREP_ID;
                        prep_bit_cnt <= 5'd0;
                    end
                    PREP_ID: begin
                        if (prep_bit_cnt == 5'd10) begin
                            prep_state <= PREP_RTR;
                            prep_bit_cnt <= 5'd0;
                        end else begin
                            prep_bit_cnt <= prep_bit_cnt + 1'b1;
                        end
                    end
                    PREP_RTR: begin
                        prep_state <= PREP_IDE;
                    end
                    PREP_IDE: begin
                        prep_state <= PREP_R0;
                    end
                    PREP_R0: begin
                        prep_state <= PREP_DLC;
                        prep_bit_cnt <= 5'd0;
                    end
                    PREP_DLC: begin
                        if (prep_bit_cnt == 5'd3) begin
                            prep_bit_cnt <= 5'd0;
                            if (tx_msg_dlc == 4'd0) begin
                                prep_state <= PREP_CRC;
                            end else begin
                                prep_state <= PREP_DATA;
                                prep_byte_cnt <= 3'd0;
                            end
                        end else begin
                            prep_bit_cnt <= prep_bit_cnt + 1'b1;
                        end
                    end
                    PREP_DATA: begin
                        if (prep_bit_cnt == 5'd7) begin
                            prep_bit_cnt <= 5'd0;
                            if (prep_byte_cnt == (tx_msg_dlc - 1'b1)) begin
                                prep_state <= PREP_CRC;
                            end else begin
                                prep_byte_cnt <= prep_byte_cnt + 1'b1;
                            end
                        end else begin
                            prep_bit_cnt <= prep_bit_cnt + 1'b1;
                        end
                    end
                    PREP_CRC: begin
                        if (prep_bit_cnt == 5'd14) begin
                            prep_state <= PREP_CRC_DELIM;
                            prep_bit_cnt <= 5'd0;
                        end else begin
                            prep_bit_cnt <= prep_bit_cnt + 1'b1;
                        end
                    end
                    PREP_CRC_DELIM: begin
                        prep_state <= PREP_ACK;
                    end
                    PREP_ACK: begin
                        prep_state <= PREP_ACK_DELIM;
                    end
                    PREP_ACK_DELIM: begin
                        prep_state <= PREP_EOF;
                        prep_bit_cnt <= 5'd0;
                    end
                    PREP_EOF: begin
                        if (prep_bit_cnt == 5'd6) begin
                            prep_state <= PREP_DONE;
                        end else begin
                            prep_bit_cnt <= prep_bit_cnt + 1'b1;
                        end
                    end
                    PREP_DONE: begin
                        prep_state <= PREP_DONE;
                    end
                    default: begin
                        prep_state <= PREP_SOF;
                    end
                endcase
            end
        end
    end
end

always @(posedge clk) begin
    if (reset) begin
        tx_active <= 1'b0;
        tx_pending <= 1'b0;
        tx_buf_pos <= 8'd0;
        tx_bit <= 1'b1;
        tx_done <= 1'b0;
        tx_ack_seen <= 1'b0;
    end else begin
        tx_done <= 1'b0;
        if (prep_done_pulse) begin
            tx_pending <= 1'b1;
        end
        if (tx_start_pulse) begin
            tx_active <= 1'b1;
            tx_pending <= 1'b0;
            tx_buf_pos <= 8'd0;
            tx_bit <= tx_buf[0];
            tx_ack_seen <= 1'b0;
        end

        if (tx_active && sample_tick) begin
            if (tx_buf_pos == tx_ack_index) begin
                if (can_rx_s == 1'b0) begin
                    tx_ack_seen <= 1'b1;
                end
            end
        end

        if (tx_active && bit_end_tick) begin
            if (tx_buf_pos == (tx_buf_len - 1'b1)) begin
                tx_active <= 1'b0;
                tx_done <= 1'b1;
                tx_bit <= 1'b1;
            end else begin
                tx_buf_pos <= tx_buf_pos + 1'b1;
                tx_bit <= tx_buf[tx_buf_pos + 1'b1];
            end
        end
    end
end

endmodule
