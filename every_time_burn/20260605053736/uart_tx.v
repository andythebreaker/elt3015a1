module uart_tx #(
    parameter integer CLK_HZ = 100_000_000,
    parameter integer BAUD = 115200
) (
    input  wire clk,
    input  wire reset,
    input  wire tx_start,
    input  wire [7:0] tx_data,
    output reg  tx_busy,
    output reg  tx_line
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

localparam integer BAUD_DIV = (CLK_HZ / BAUD);
localparam integer BAUD_CNT_W = (BAUD_DIV <= 1) ? 1 : clog2(BAUD_DIV);

localparam [1:0] S_IDLE  = 2'd0;
localparam [1:0] S_START = 2'd1;
localparam [1:0] S_DATA  = 2'd2;
localparam [1:0] S_STOP  = 2'd3;

reg [1:0] state = S_IDLE;
reg [BAUD_CNT_W-1:0] baud_cnt = {BAUD_CNT_W{1'b0}};
reg [2:0] bit_idx = 3'd0;
reg [7:0] shifter = 8'd0;
reg stop_bit_cnt = 1'b0;
wire baud_tick = (BAUD_DIV <= 1) ? 1'b1 : (baud_cnt == BAUD_DIV - 1);

always @(posedge clk) begin
    if (reset) begin
        state <= S_IDLE;
        baud_cnt <= {BAUD_CNT_W{1'b0}};
        bit_idx <= 3'd0;
        shifter <= 8'd0;
        stop_bit_cnt <= 1'b0;
        tx_busy <= 1'b0;
        tx_line <= 1'b1;
    end else begin
        if (BAUD_DIV > 1) begin
            if (state != S_IDLE && baud_tick) begin
                baud_cnt <= {BAUD_CNT_W{1'b0}};
            end else if (state != S_IDLE) begin
                baud_cnt <= baud_cnt + 1'b1;
            end
        end

        case (state)
            S_IDLE: begin
                tx_busy <= 1'b0;
                tx_line <= 1'b1;
                baud_cnt <= {BAUD_CNT_W{1'b0}};
                stop_bit_cnt <= 1'b0;
                if (tx_start) begin
                    shifter <= tx_data;
                    bit_idx <= 3'd0;
                    tx_busy <= 1'b1;
                    tx_line <= 1'b0;
                    state <= S_START;
                end
            end
            S_START: begin
                if (baud_tick) begin
                    tx_line <= shifter[0];
                    shifter <= {1'b0, shifter[7:1]};
                    bit_idx <= 3'd0;
                    state <= S_DATA;
                end
            end
            S_DATA: begin
                if (baud_tick) begin
                    if (bit_idx == 3'd7) begin
                        tx_line <= 1'b1;
                        state <= S_STOP;
                    end else begin
                        tx_line <= shifter[0];
                        shifter <= {1'b0, shifter[7:1]};
                        bit_idx <= bit_idx + 1'b1;
                    end
                end
            end
            S_STOP: begin
                if (baud_tick) begin
                    if (stop_bit_cnt == 1'b1) begin
                        tx_line <= 1'b1;
                        tx_busy <= 1'b0;
                        state <= S_IDLE;
                    end else begin
                        stop_bit_cnt <= 1'b1;
                    end
                end
            end
            default: begin
                state <= S_IDLE;
                tx_busy <= 1'b0;
                tx_line <= 1'b1;
            end
        endcase
    end
end

endmodule
